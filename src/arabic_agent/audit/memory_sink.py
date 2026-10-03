"""In-memory audit sink for the local runtime and tests."""

from __future__ import annotations

import asyncio

from .base import AuditEvent, AuditSink


class InMemoryAuditSink(AuditSink):
    """Keeps audit events in a bounded, per-run ring buffer."""

    def __init__(self, *, max_events_per_run: int = 10_000) -> None:
        self._events: dict[tuple[str, str], list[AuditEvent]] = {}
        self._max = max_events_per_run
        self._lock = asyncio.Lock()

    async def next_sequence(self, *, run_id: str) -> int:
        # This sink is process-local, so it can assign a per-run sequence from
        # the events it already holds. That keeps sequences unique even when no
        # durable sink (Postgres) is attached.
        for (_, rid), bucket in self._events.items():
            if rid == run_id and bucket:
                return bucket[-1].sequence + 1
        return 0

    async def emit(self, event: AuditEvent) -> None:
        key = (event.tenant_id, event.run_id)
        async with self._lock:
            bucket = self._events.setdefault(key, [])
            bucket.append(event)
            if len(bucket) > self._max:
                del bucket[: len(bucket) - self._max]

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        async with self._lock:
            return list(self._events.get((tenant_id, run_id), ()))
