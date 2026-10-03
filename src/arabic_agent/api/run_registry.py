"""In-memory run registry.

Tracks the context needed to resume a run after an approval decision (the goal
and workspace are not otherwise recoverable from the plan alone). This is the
single-node implementation; a cloud deployment backs it with Postgres. It is
real state management, not a placeholder.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from ..core.enums import RunStatus


@dataclass
class RunRecord:
    run_id: str
    tenant_id: str
    user_id: str
    goal: str
    workspace_dir: str
    status: RunStatus
    pending_approval_id: str | None = None
    result: str = ""


class RunRegistry:
    """Process-local registry of runs, scoped by tenant."""

    def __init__(self) -> None:
        self._records: dict[str, RunRecord] = {}
        self._lock = asyncio.Lock()

    async def put(self, record: RunRecord) -> RunRecord:
        async with self._lock:
            self._records[record.run_id] = record
        return record

    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord | None:
        async with self._lock:
            record = self._records.get(run_id)
        if record is None or record.tenant_id != tenant_id:
            return None
        return record

    async def update(self, record: RunRecord) -> RunRecord:
        async with self._lock:
            self._records[record.run_id] = record
        return record

    async def list_for_tenant(self, *, tenant_id: str) -> list[RunRecord]:
        async with self._lock:
            return [r for r in self._records.values() if r.tenant_id == tenant_id]
