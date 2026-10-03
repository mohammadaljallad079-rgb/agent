"""Task engine.

Owns the lifecycle of a plan: create it (from a planner or a fixed list),
persist it, advance steps, record outcomes, and revise it when the agent
replans. Keeping this logic out of the agent loop keeps the loop readable and
makes plan mutations testable in isolation.
"""

from __future__ import annotations

from datetime import UTC, datetime

from ..core.enums import ErrorClass, StepStatus
from .base import TaskPlan, TaskStep
from .store import TaskStore


class TaskEngine:
    """Coordinates task plan state transitions and persistence."""

    def __init__(self, store: TaskStore) -> None:
        self._store = store

    async def create_plan(self, plan: TaskPlan) -> TaskPlan:
        return await self._store.save(plan)

    async def load(self, plan_id: str, *, tenant_id: str) -> TaskPlan:
        return await self._store.get(plan_id, tenant_id=tenant_id)

    async def load_by_run(self, *, tenant_id: str, run_id: str) -> TaskPlan | None:
        return await self._store.get_by_run(tenant_id=tenant_id, run_id=run_id)

    async def start_step(self, plan: TaskPlan, step_id: str) -> TaskPlan:
        step = self._require_step(plan, step_id)
        updated = step.model_copy(
            update={
                "status": StepStatus.IN_PROGRESS,
                "attempts": step.attempts + 1,
                "started_at": step.started_at or _now(),
            }
        )
        return await self._store.save(plan.with_step(updated))

    async def complete_step(
        self, plan: TaskPlan, step_id: str, *, summary: str, data: dict[str, object] | None = None
    ) -> TaskPlan:
        step = self._require_step(plan, step_id)
        updated = step.model_copy(
            update={
                "status": StepStatus.DONE,
                "result_summary": summary,
                "result_data": data or {},
                "error_class": None,
                "error_message": None,
                "finished_at": _now(),
            }
        )
        return await self._store.save(plan.with_step(updated))

    async def fail_step(
        self,
        plan: TaskPlan,
        step_id: str,
        *,
        error_class: ErrorClass,
        message: str,
    ) -> TaskPlan:
        step = self._require_step(plan, step_id)
        updated = step.model_copy(
            update={
                "status": StepStatus.FAILED,
                "error_class": error_class,
                "error_message": message,
                "finished_at": _now(),
            }
        )
        return await self._store.save(plan.with_step(updated))

    async def block_step(self, plan: TaskPlan, step_id: str, *, reason: str) -> TaskPlan:
        step = self._require_step(plan, step_id)
        updated = step.model_copy(update={"status": StepStatus.BLOCKED, "error_message": reason})
        return await self._store.save(plan.with_step(updated))

    async def skip_step(self, plan: TaskPlan, step_id: str, *, reason: str) -> TaskPlan:
        step = self._require_step(plan, step_id)
        updated = step.model_copy(update={"status": StepStatus.SKIPPED, "result_summary": reason})
        return await self._store.save(plan.with_step(updated))

    async def replan(self, plan: TaskPlan, *, new_steps: list[TaskStep], reason: str) -> TaskPlan:
        """Replace the remaining steps while preserving completed ones."""
        completed = tuple(s for s in plan.steps if s.status is StepStatus.DONE)
        renumbered = tuple(
            step.model_copy(update={"index": len(completed) + i})
            for i, step in enumerate(new_steps)
        )
        revised = plan.model_copy(
            update={
                "steps": completed + renumbered,
                "revision": plan.revision + 1,
                "updated_at": _now(),
            }
        )
        return await self._store.save(revised)

    @staticmethod
    def _require_step(plan: TaskPlan, step_id: str) -> TaskStep:
        for step in plan.steps:
            if step.id == step_id:
                return step
        from ..core.errors import NotFoundError

        raise NotFoundError("task step not found", details={"step_id": step_id})


def _now() -> datetime:
    return datetime.now(UTC)
