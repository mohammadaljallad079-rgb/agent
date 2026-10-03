"""Task persistence."""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod

from ..core.errors import NotFoundError
from .base import TaskPlan


class TaskStore(ABC):
    """Persistence contract for task plans (enables crash recovery/resume)."""

    @abstractmethod
    async def save(self, plan: TaskPlan) -> TaskPlan: ...

    @abstractmethod
    async def get(self, plan_id: str, *, tenant_id: str) -> TaskPlan: ...

    @abstractmethod
    async def get_by_run(self, *, tenant_id: str, run_id: str) -> TaskPlan | None: ...


class InMemoryTaskStore(TaskStore):
    """Process-local task store for the local runtime and tests."""

    def __init__(self) -> None:
        self._by_id: dict[str, TaskPlan] = {}
        self._by_run: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    async def save(self, plan: TaskPlan) -> TaskPlan:
        async with self._lock:
            self._by_id[plan.id] = plan
            self._by_run[(plan.tenant_id, plan.run_id)] = plan.id
        return plan

    async def get(self, plan_id: str, *, tenant_id: str) -> TaskPlan:
        async with self._lock:
            plan = self._by_id.get(plan_id)
        if plan is None or plan.tenant_id != tenant_id:
            raise NotFoundError("task plan not found", details={"plan_id": plan_id})
        return plan

    async def get_by_run(self, *, tenant_id: str, run_id: str) -> TaskPlan | None:
        async with self._lock:
            plan_id = self._by_run.get((tenant_id, run_id))
            return self._by_id.get(plan_id) if plan_id else None
