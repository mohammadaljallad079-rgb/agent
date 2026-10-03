"""Postgres-backed task store.

A plan and its steps are written together inside one transaction, so a plan can
never be observed with a partial step list after a crash. Steps removed by a
replan are deleted; the rest are upserted.
"""

from __future__ import annotations

from sqlalchemy import delete, select

from ...core.errors import NotFoundError
from ...persistence.database import Database
from ...persistence.mapping import plan_from_row, plan_to_row, step_to_row
from ...persistence.schema import TaskPlanRow, TaskStepRow
from ...tasks.base import TaskPlan
from ...tasks.store import TaskStore


class PostgresTaskStore(TaskStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, plan: TaskPlan) -> TaskPlan:
        async with self._db.transaction() as session:
            row = await session.get(TaskPlanRow, plan.id)
            if row is None:
                row = TaskPlanRow(**plan_to_row(plan))
                session.add(row)
                await session.flush()
            else:
                row.tenant_id = plan.tenant_id
                row.user_id = plan.user_id
                row.run_id = plan.run_id
                row.goal = plan.goal
                row.revision = plan.revision
                row.updated_at = plan.updated_at

            existing_ids = {
                s.id
                for s in (
                    await session.execute(select(TaskStepRow).where(TaskStepRow.plan_id == plan.id))
                )
                .scalars()
                .all()
            }
            desired_ids = {step.id for step in plan.steps}
            removed = existing_ids - desired_ids
            if removed:
                await session.execute(
                    delete(TaskStepRow).where(
                        TaskStepRow.plan_id == plan.id, TaskStepRow.id.in_(removed)
                    )
                )
            for step in plan.steps:
                step_row = await session.get(TaskStepRow, step.id)
                if step_row is None:
                    session.add(TaskStepRow(**step_to_row(plan.id, plan.tenant_id, step)))
                else:
                    for key, value in step_to_row(plan.id, plan.tenant_id, step).items():
                        setattr(step_row, key, value)
        return plan

    async def get(self, plan_id: str, *, tenant_id: str) -> TaskPlan:
        async with self._db.transaction() as session:
            row = await session.get(TaskPlanRow, plan_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("task plan not found", details={"plan_id": plan_id})
        return plan_from_row(row)

    async def get_by_run(self, *, tenant_id: str, run_id: str) -> TaskPlan | None:
        async with self._db.transaction() as session:
            row = (
                await session.execute(
                    select(TaskPlanRow).where(
                        TaskPlanRow.tenant_id == tenant_id, TaskPlanRow.run_id == run_id
                    )
                )
            ).scalar_one_or_none()
        return plan_from_row(row) if row else None
