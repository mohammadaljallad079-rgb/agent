"""Trigger persistence contracts and in-memory implementations.

Two stores, both tenant-scoped and both using optimistic concurrency:

* :class:`TriggerStore` — trigger definitions plus their scheduler lease state.
* :class:`TriggerRunStore` — the firing history, keyed by an idempotency key so
  a redelivered webhook or a crashed scheduler produces one run, not many.

The in-memory implementations mirror the Postgres semantics exactly, including
optimistic-concurrency conflicts and the uniqueness of idempotency keys.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime

from ..core.enums import TriggerKind
from ..core.errors import ConflictError, NotFoundError
from .models import Trigger, TriggerRun


class TriggerStore(ABC):
    """Persistence for trigger definitions and their lease state."""

    @abstractmethod
    async def create(self, trigger: Trigger) -> Trigger: ...

    @abstractmethod
    async def get(self, trigger_id: str, *, tenant_id: str) -> Trigger: ...

    @abstractmethod
    async def update(self, trigger: Trigger, *, expected_version: int) -> Trigger: ...

    @abstractmethod
    async def delete(self, trigger_id: str, *, tenant_id: str) -> None: ...

    @abstractmethod
    async def list_triggers(
        self,
        *,
        tenant_id: str,
        kind: TriggerKind | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Trigger]: ...

    @abstractmethod
    async def get_by_webhook_id(self, webhook_id: str) -> Trigger | None:
        """Look up a webhook trigger by its public id (tenant resolved after)."""

    @abstractmethod
    async def list_event_triggers(self, *, tenant_id: str, event_type: str) -> list[Trigger]:
        """Enabled event triggers subscribed to ``event_type`` within a tenant."""

    @abstractmethod
    async def list_due(self, *, now: datetime, limit: int = 100) -> list[Trigger]:
        """Enabled cron triggers whose ``next_fire_at`` is due and lease-free."""

    @abstractmethod
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

        Returns the updated trigger if this caller won the claim, else ``None``.
        This single conditional update is the scheduler's concurrency primitive:
        only one scheduler racing a given occurrence can advance the row.
        """


class TriggerRunStore(ABC):
    """Persistence for trigger firing history (idempotent per tenant)."""

    @abstractmethod
    async def create(self, run: TriggerRun) -> TriggerRun: ...

    @abstractmethod
    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> TriggerRun | None: ...

    @abstractmethod
    async def list_for_trigger(
        self, *, tenant_id: str, trigger_id: str, limit: int = 50
    ) -> list[TriggerRun]: ...


class InMemoryTriggerStore(TriggerStore):
    """Process-local trigger store for the local runtime and tests."""

    def __init__(self) -> None:
        self._by_id: dict[str, Trigger] = {}
        self._lock = asyncio.Lock()

    async def create(self, trigger: Trigger) -> Trigger:
        async with self._lock:
            self._by_id[trigger.id] = trigger
        return trigger

    async def get(self, trigger_id: str, *, tenant_id: str) -> Trigger:
        async with self._lock:
            trigger = self._by_id.get(trigger_id)
        if trigger is None or trigger.tenant_id != tenant_id:
            raise NotFoundError("trigger not found", details={"trigger_id": trigger_id})
        return trigger

    async def update(self, trigger: Trigger, *, expected_version: int) -> Trigger:
        async with self._lock:
            current = self._by_id.get(trigger.id)
            if current is None or current.tenant_id != trigger.tenant_id:
                raise NotFoundError("trigger not found", details={"trigger_id": trigger.id})
            if current.version != expected_version:
                raise ConflictError(
                    "trigger was modified concurrently",
                    details={"trigger_id": trigger.id, "expected": expected_version},
                )
            updated = trigger.model_copy(update={"version": expected_version + 1})
            self._by_id[trigger.id] = updated
        return updated

    async def delete(self, trigger_id: str, *, tenant_id: str) -> None:
        async with self._lock:
            trigger = self._by_id.get(trigger_id)
            if trigger is None or trigger.tenant_id != tenant_id:
                raise NotFoundError("trigger not found", details={"trigger_id": trigger_id})
            del self._by_id[trigger_id]

    async def list_triggers(
        self,
        *,
        tenant_id: str,
        kind: TriggerKind | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Trigger]:
        async with self._lock:
            items = [
                t
                for t in self._by_id.values()
                if t.tenant_id == tenant_id
                and (kind is None or t.kind is kind)
                and (workflow_id is None or t.workflow_id == workflow_id)
            ]
        items.sort(key=lambda t: t.created_at, reverse=True)
        return items[offset : offset + limit]

    async def get_by_webhook_id(self, webhook_id: str) -> Trigger | None:
        async with self._lock:
            for trigger in self._by_id.values():
                if trigger.webhook_id == webhook_id:
                    return trigger
        return None

    async def list_event_triggers(self, *, tenant_id: str, event_type: str) -> list[Trigger]:
        async with self._lock:
            items = [
                t
                for t in self._by_id.values()
                if t.tenant_id == tenant_id
                and t.enabled
                and t.kind is TriggerKind.EVENT
                and t.event_type == event_type
            ]
        return items

    async def list_due(self, *, now: datetime, limit: int = 100) -> list[Trigger]:
        async with self._lock:
            items = [t for t in self._by_id.values() if t.is_due(now) and t.lease_free(now)]
        items.sort(key=lambda t: t.next_fire_at or t.created_at)
        return items[:limit]

    async def claim_due(
        self,
        trigger_id: str,
        *,
        now: datetime,
        next_fire_at: datetime,
        owner: str,
        lease_expires_at: datetime,
    ) -> Trigger | None:
        async with self._lock:
            current = self._by_id.get(trigger_id)
            if (
                current is None
                or not current.enabled
                or current.kind is not TriggerKind.CRON
                or current.next_fire_at is None
                or current.next_fire_at > now
                or not current.lease_free(now)
            ):
                return None
            updated = current.model_copy(
                update={
                    "next_fire_at": next_fire_at,
                    "lease_owner": owner,
                    "lease_expires_at": lease_expires_at,
                    "version": current.version + 1,
                }
            )
            self._by_id[trigger_id] = updated
        return updated


class InMemoryTriggerRunStore(TriggerRunStore):
    """Process-local trigger firing history."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], TriggerRun] = {}
        self._lock = asyncio.Lock()

    async def create(self, run: TriggerRun) -> TriggerRun:
        async with self._lock:
            key = (run.tenant_id, run.idempotency_key)
            if key in self._by_key:
                raise ConflictError(
                    "trigger run already recorded for this idempotency key",
                    details={"idempotency_key": run.idempotency_key},
                )
            self._by_key[key] = run
        return run

    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> TriggerRun | None:
        async with self._lock:
            return self._by_key.get((tenant_id, key))

    async def list_for_trigger(
        self, *, tenant_id: str, trigger_id: str, limit: int = 50
    ) -> list[TriggerRun]:
        async with self._lock:
            items = [
                r
                for r in self._by_key.values()
                if r.tenant_id == tenant_id and r.trigger_id == trigger_id
            ]
        items.sort(key=lambda r: r.created_at, reverse=True)
        return items[:limit]
