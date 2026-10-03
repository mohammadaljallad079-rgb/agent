"""Run persistence contract and in-memory implementation."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime

from ..core.enums import RunStatus
from ..core.errors import ConflictError, NotFoundError
from .base import RunRecord


class RunStore(ABC):
    """Persistence contract for agent runs.

    ``update`` is guarded by ``expected_version`` (optimistic concurrency): a
    caller that read version *n* can only write version *n+1*. This is what
    stops a stale or duplicated worker from clobbering newer state.
    """

    @abstractmethod
    async def create(self, record: RunRecord) -> RunRecord: ...

    @abstractmethod
    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord: ...

    @abstractmethod
    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> RunRecord | None: ...

    @abstractmethod
    async def update(self, record: RunRecord, *, expected_version: int) -> RunRecord: ...

    @abstractmethod
    async def list_runs(
        self,
        *,
        tenant_id: str,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]: ...

    @abstractmethod
    async def list_stale(self, *, now: datetime, limit: int = 100) -> list[RunRecord]:
        """Runs whose worker lease has expired and that are not terminal."""

    @abstractmethod
    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        """Renew a worker's lease on a run without changing the run's version.

        A heartbeat is not a state transition; it must not disturb optimistic
        concurrency, so it updates only the lease columns.
        """


class InMemoryRunStore(RunStore):
    """Process-local run store for the local/offline runtime and tests.

    Semantics match the Postgres store (including optimistic concurrency), so
    behaviour is identical whether persistence is in memory or in Postgres.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, record: RunRecord) -> RunRecord:
        async with self._lock:
            self._by_id[record.id] = record
        return record

    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord:
        async with self._lock:
            record = self._by_id.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFoundError("run not found", details={"run_id": run_id})
        return record

    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> RunRecord | None:
        async with self._lock:
            for record in self._by_id.values():
                if record.tenant_id == tenant_id and record.idempotency_key == key:
                    return record
        return None

    async def update(self, record: RunRecord, *, expected_version: int) -> RunRecord:
        async with self._lock:
            current = self._by_id.get(record.id)
            if current is None or current.tenant_id != record.tenant_id:
                raise NotFoundError("run not found", details={"run_id": record.id})
            if current.version != expected_version:
                raise ConflictError(
                    "run was modified concurrently",
                    details={
                        "run_id": record.id,
                        "expected": expected_version,
                        "actual": current.version,
                    },
                )
            updated = record.model_copy(update={"version": expected_version + 1})
            self._by_id[record.id] = updated
        return updated

    async def list_runs(
        self,
        *,
        tenant_id: str,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        async with self._lock:
            records = [
                r
                for r in self._by_id.values()
                if r.tenant_id == tenant_id and (status is None or r.status is status)
            ]
        records.sort(key=lambda r: r.created_at, reverse=True)
        return records[offset : offset + limit]

    async def list_stale(self, *, now: datetime, limit: int = 100) -> list[RunRecord]:
        async with self._lock:
            records = [
                r
                for r in self._by_id.values()
                if r.is_recoverable
                and r.status is not RunStatus.PENDING
                and (r.lease_expires_at is None or r.lease_expires_at <= now)
            ]
        records.sort(key=lambda r: r.updated_at)
        return records[:limit]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._lock:
            current = self._by_id.get(run_id)
            if current is None or current.tenant_id != tenant_id:
                return
            self._by_id[run_id] = current.model_copy(
                update={"worker_id": worker_id, "lease_expires_at": lease_expires_at}
            )
