"""Postgres-backed run store.

Implements :class:`~arabic_agent.runs.store.RunStore` with optimistic
concurrency. The update is a single conditional ``UPDATE ... WHERE version = :n``
statement, so two workers racing on the same run cannot both win: the loser sees
a rowcount of 0 and receives a :class:`ConflictError`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from ...core.enums import RunStatus
from ...core.errors import ConflictError, NotFoundError
from ...persistence.database import Database
from ...persistence.mapping import run_from_row, run_to_row
from ...persistence.schema import RunRow
from ...runs.base import RunRecord
from ...runs.store import RunStore


class PostgresRunStore(RunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, record: RunRecord) -> RunRecord:
        try:
            async with self._db.transaction() as session:
                session.add(RunRow(**run_to_row(record)))
        except IntegrityError as exc:
            raise ConflictError(
                "run with this idempotency key already exists",
                details={"idempotency_key": record.idempotency_key},
            ) from exc
        return record

    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord:
        async with self._db.transaction() as session:
            row = await session.get(RunRow, run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("run not found", details={"run_id": run_id})
        return run_from_row(row)

    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> RunRecord | None:
        async with self._db.transaction() as session:
            row = (
                await session.execute(
                    select(RunRow).where(
                        RunRow.tenant_id == tenant_id, RunRow.idempotency_key == key
                    )
                )
            ).scalar_one_or_none()
        return run_from_row(row) if row else None

    async def update(self, record: RunRecord, *, expected_version: int) -> RunRecord:
        values = run_to_row(record)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(RunRow)
                .where(
                    RunRow.id == record.id,
                    RunRow.tenant_id == record.tenant_id,
                    RunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(RunRow, record.id)
                if existing is None or existing.tenant_id != record.tenant_id:
                    raise NotFoundError("run not found", details={"run_id": record.id})
                raise ConflictError(
                    "run was modified concurrently",
                    details={"run_id": record.id, "expected": expected_version},
                )
            row = await session.get(RunRow, record.id)
        assert row is not None
        return run_from_row(row)

    async def list_runs(
        self,
        *,
        tenant_id: str,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        stmt = select(RunRow).where(RunRow.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(RunRow.status == status.value)
        stmt = stmt.order_by(RunRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [run_from_row(r) for r in rows]

    async def list_stale(self, *, now: datetime, limit: int = 100) -> list[RunRecord]:
        stmt = (
            select(RunRow)
            .where(
                RunRow.status.in_(
                    [
                        RunStatus.PLANNING.value,
                        RunStatus.RUNNING.value,
                        RunStatus.RETRYING.value,
                    ]
                ),
                (RunRow.lease_expires_at.is_(None)) | (RunRow.lease_expires_at <= now),
            )
            .order_by(RunRow.updated_at)
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [run_from_row(r) for r in rows]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                update(RunRow)
                .where(RunRow.id == run_id, RunRow.tenant_id == tenant_id)
                .values(worker_id=worker_id, lease_expires_at=lease_expires_at)
            )
