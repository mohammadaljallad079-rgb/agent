"""Postgres-backed trigger stores.

Two implementations of the trigger persistence contracts. The important one is
:meth:`PostgresTriggerStore.claim_due`: the scheduler's claim is a single
conditional ``UPDATE`` that only succeeds if the trigger is still due and its
lease is free, and it advances ``next_fire_at`` and takes the lease atomically.
That single statement is what makes running many schedulers safe — two
schedulers racing the same occurrence cannot both win, because only one UPDATE
affects a row.

Idempotent firing history relies on the ``(tenant_id, idempotency_key)`` unique
constraint; a duplicate insert raises :class:`ConflictError` rather than
creating a second run.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import delete, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from ...core.enums import TriggerKind
from ...core.errors import ConflictError, NotFoundError
from ...persistence.database import Database
from ...persistence.mapping import (
    trigger_from_row,
    trigger_run_from_row,
    trigger_run_to_row,
    trigger_to_row,
)
from ...persistence.schema import TriggerRow, TriggerRunRow
from ...triggers.models import Trigger, TriggerRun
from ...triggers.store import TriggerRunStore, TriggerStore


class PostgresTriggerStore(TriggerStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, trigger: Trigger) -> Trigger:
        try:
            async with self._db.transaction() as session:
                session.add(TriggerRow(**trigger_to_row(trigger)))
        except IntegrityError as exc:
            raise ConflictError(
                "trigger already exists",
                details={"webhook_id": trigger.webhook_id},
            ) from exc
        return trigger

    async def get(self, trigger_id: str, *, tenant_id: str) -> Trigger:
        async with self._db.transaction() as session:
            row = await session.get(TriggerRow, trigger_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("trigger not found", details={"trigger_id": trigger_id})
        return trigger_from_row(row)

    async def update(self, trigger: Trigger, *, expected_version: int) -> Trigger:
        values = trigger_to_row(trigger)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(TriggerRow)
                .where(
                    TriggerRow.id == trigger.id,
                    TriggerRow.tenant_id == trigger.tenant_id,
                    TriggerRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(TriggerRow, trigger.id)
                if existing is None or existing.tenant_id != trigger.tenant_id:
                    raise NotFoundError("trigger not found", details={"trigger_id": trigger.id})
                raise ConflictError(
                    "trigger was modified concurrently",
                    details={"trigger_id": trigger.id, "expected": expected_version},
                )
            row = await session.get(TriggerRow, trigger.id)
        assert row is not None
        return trigger_from_row(row)

    async def delete(self, trigger_id: str, *, tenant_id: str) -> None:
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(TriggerRow).where(
                    TriggerRow.id == trigger_id, TriggerRow.tenant_id == tenant_id
                )
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                raise NotFoundError("trigger not found", details={"trigger_id": trigger_id})

    async def list_triggers(
        self,
        *,
        tenant_id: str,
        kind: TriggerKind | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Trigger]:
        stmt = select(TriggerRow).where(TriggerRow.tenant_id == tenant_id)
        if kind is not None:
            stmt = stmt.where(TriggerRow.kind == kind.value)
        if workflow_id is not None:
            stmt = stmt.where(TriggerRow.workflow_id == workflow_id)
        stmt = stmt.order_by(TriggerRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [trigger_from_row(r) for r in rows]

    async def get_by_webhook_id(self, webhook_id: str) -> Trigger | None:
        stmt = select(TriggerRow).where(TriggerRow.webhook_id == webhook_id)
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return trigger_from_row(row) if row else None

    async def list_event_triggers(self, *, tenant_id: str, event_type: str) -> list[Trigger]:
        stmt = select(TriggerRow).where(
            TriggerRow.tenant_id == tenant_id,
            TriggerRow.enabled.is_(True),
            TriggerRow.kind == TriggerKind.EVENT.value,
            TriggerRow.event_type == event_type,
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [trigger_from_row(r) for r in rows]

    async def list_due(self, *, now: datetime, limit: int = 100) -> list[Trigger]:
        stmt = (
            select(TriggerRow)
            .where(
                TriggerRow.enabled.is_(True),
                TriggerRow.kind == TriggerKind.CRON.value,
                TriggerRow.next_fire_at.is_not(None),
                TriggerRow.next_fire_at <= now,
                or_(
                    TriggerRow.lease_expires_at.is_(None),
                    TriggerRow.lease_expires_at <= now,
                ),
            )
            .order_by(TriggerRow.next_fire_at)
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [trigger_from_row(r) for r in rows]

    async def claim_due(
        self,
        trigger_id: str,
        *,
        now: datetime,
        next_fire_at: datetime,
        owner: str,
        lease_expires_at: datetime,
    ) -> Trigger | None:
        """Atomically claim a due trigger and advance its schedule.

        Returns the updated trigger if this caller won the claim, else ``None``
        (another scheduler claimed it, it is no longer due, or it is leased).
        This is the only method beyond the ABC; it is the scheduler's primitive.
        """
        async with self._db.transaction() as session:
            result = await session.execute(
                update(TriggerRow)
                .where(
                    TriggerRow.id == trigger_id,
                    TriggerRow.enabled.is_(True),
                    TriggerRow.kind == TriggerKind.CRON.value,
                    TriggerRow.next_fire_at.is_not(None),
                    TriggerRow.next_fire_at <= now,
                    or_(
                        TriggerRow.lease_expires_at.is_(None),
                        TriggerRow.lease_expires_at <= now,
                    ),
                )
                .values(
                    next_fire_at=next_fire_at,
                    lease_owner=owner,
                    lease_expires_at=lease_expires_at,
                    version=TriggerRow.version + 1,
                )
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                return None
            row = await session.get(TriggerRow, trigger_id)
        assert row is not None
        return trigger_from_row(row)


class PostgresTriggerRunStore(TriggerRunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, run: TriggerRun) -> TriggerRun:
        try:
            async with self._db.transaction() as session:
                session.add(TriggerRunRow(**trigger_run_to_row(run)))
        except IntegrityError as exc:
            raise ConflictError(
                "trigger run already recorded for this idempotency key",
                details={"idempotency_key": run.idempotency_key},
            ) from exc
        return run

    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> TriggerRun | None:
        stmt = select(TriggerRunRow).where(
            TriggerRunRow.tenant_id == tenant_id,
            TriggerRunRow.idempotency_key == key,
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return trigger_run_from_row(row) if row else None

    async def list_for_trigger(
        self, *, tenant_id: str, trigger_id: str, limit: int = 50
    ) -> list[TriggerRun]:
        stmt = (
            select(TriggerRunRow)
            .where(
                TriggerRunRow.tenant_id == tenant_id,
                TriggerRunRow.trigger_id == trigger_id,
            )
            .order_by(TriggerRunRow.created_at.desc())
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [trigger_run_from_row(r) for r in rows]
