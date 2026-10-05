"""Postgres-backed workflow stores.

Three implementations of the workflow persistence contracts, all following the
same patterns as the run/task stores: a single conditional
``UPDATE ... WHERE version = :n`` for optimistic concurrency, tenant-scoped
queries, and row <-> domain mapping delegated to ``persistence.mapping``.

Version rows are immutable and are never updated, which is what guarantees that
a running workflow's pinned definition can never change underneath it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from ...core.enums import StepRunStatus, WorkflowRunStatus
from ...core.errors import ConflictError, NotFoundError
from ...persistence.database import Database
from ...persistence.mapping import (
    step_run_from_row,
    step_run_to_row,
    workflow_from_row,
    workflow_run_from_row,
    workflow_run_to_row,
    workflow_to_row,
    workflow_version_from_row,
    workflow_version_to_row,
)
from ...persistence.schema import (
    WorkflowRow,
    WorkflowRunRow,
    WorkflowStepRunRow,
    WorkflowVersionRow,
)
from ...workflows.models import Workflow, WorkflowVersion
from ...workflows.run import StepRun, WorkflowRun
from ...workflows.store import WorkflowRunStore, WorkflowStore, WorkflowVersionStore


class PostgresWorkflowStore(WorkflowStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, workflow: Workflow) -> Workflow:
        try:
            async with self._db.transaction() as session:
                session.add(WorkflowRow(**workflow_to_row(workflow)))
        except IntegrityError as exc:
            raise ConflictError(
                "a workflow with this name already exists",
                details={"name": workflow.name},
            ) from exc
        return workflow

    async def get(self, workflow_id: str, *, tenant_id: str) -> Workflow:
        async with self._db.transaction() as session:
            row = await session.get(WorkflowRow, workflow_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow not found", details={"workflow_id": workflow_id})
        return workflow_from_row(row)

    async def update(self, workflow: Workflow, *, expected_version: int) -> Workflow:
        values = workflow_to_row(workflow)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(WorkflowRow)
                .where(
                    WorkflowRow.id == workflow.id,
                    WorkflowRow.tenant_id == workflow.tenant_id,
                    WorkflowRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(WorkflowRow, workflow.id)
                if existing is None or existing.tenant_id != workflow.tenant_id:
                    raise NotFoundError("workflow not found", details={"workflow_id": workflow.id})
                raise ConflictError(
                    "workflow was modified concurrently",
                    details={"workflow_id": workflow.id, "expected": expected_version},
                )
            row = await session.get(WorkflowRow, workflow.id)
        assert row is not None
        return workflow_from_row(row)

    async def list_workflows(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[Workflow]:
        stmt = (
            select(WorkflowRow)
            .where(WorkflowRow.tenant_id == tenant_id)
            .order_by(WorkflowRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_from_row(r) for r in rows]


class PostgresWorkflowVersionStore(WorkflowVersionStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(self, version: WorkflowVersion) -> WorkflowVersion:
        try:
            async with self._db.transaction() as session:
                session.add(WorkflowVersionRow(**workflow_version_to_row(version)))
        except IntegrityError as exc:
            raise ConflictError(
                "workflow version already exists",
                details={"workflow_id": version.workflow_id, "version": version.version},
            ) from exc
        return version

    async def get(self, version_id: str, *, tenant_id: str) -> WorkflowVersion:
        async with self._db.transaction() as session:
            row = await session.get(WorkflowVersionRow, version_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow version not found", details={"version_id": version_id})
        return workflow_version_from_row(row)

    async def latest(self, *, tenant_id: str, workflow_id: str) -> WorkflowVersion | None:
        stmt = (
            select(WorkflowVersionRow)
            .where(
                WorkflowVersionRow.tenant_id == tenant_id,
                WorkflowVersionRow.workflow_id == workflow_id,
            )
            .order_by(WorkflowVersionRow.version.desc())
            .limit(1)
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return workflow_version_from_row(row) if row else None

    async def list_versions(self, *, tenant_id: str, workflow_id: str) -> list[WorkflowVersion]:
        stmt = (
            select(WorkflowVersionRow)
            .where(
                WorkflowVersionRow.tenant_id == tenant_id,
                WorkflowVersionRow.workflow_id == workflow_id,
            )
            .order_by(WorkflowVersionRow.version.desc())
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_version_from_row(r) for r in rows]


class PostgresWorkflowRunStore(WorkflowRunStore):
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create_run(self, run: WorkflowRun, steps: list[StepRun]) -> WorkflowRun:
        try:
            async with self._db.transaction() as session:
                session.add(WorkflowRunRow(**workflow_run_to_row(run)))
                await session.flush()
                for step in steps:
                    session.add(WorkflowStepRunRow(**step_run_to_row(step)))
        except IntegrityError as exc:
            raise ConflictError(
                "workflow run with this idempotency key already exists",
                details={"idempotency_key": run.idempotency_key},
            ) from exc
        return run

    async def get_run(self, run_id: str, *, tenant_id: str) -> WorkflowRun:
        async with self._db.transaction() as session:
            row = await session.get(WorkflowRunRow, run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow run not found", details={"workflow_run_id": run_id})
        return workflow_run_from_row(row)

    async def get_run_by_idempotency_key(self, *, tenant_id: str, key: str) -> WorkflowRun | None:
        stmt = select(WorkflowRunRow).where(
            WorkflowRunRow.tenant_id == tenant_id, WorkflowRunRow.idempotency_key == key
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return workflow_run_from_row(row) if row else None

    async def update_run(self, run: WorkflowRun, *, expected_version: int) -> WorkflowRun:
        values = workflow_run_to_row(run)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(WorkflowRunRow)
                .where(
                    WorkflowRunRow.id == run.id,
                    WorkflowRunRow.tenant_id == run.tenant_id,
                    WorkflowRunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(WorkflowRunRow, run.id)
                if existing is None or existing.tenant_id != run.tenant_id:
                    raise NotFoundError(
                        "workflow run not found", details={"workflow_run_id": run.id}
                    )
                raise ConflictError(
                    "workflow run was modified concurrently",
                    details={"workflow_run_id": run.id, "expected": expected_version},
                )
            row = await session.get(WorkflowRunRow, run.id)
        assert row is not None
        return workflow_run_from_row(row)

    async def list_runs(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        status: WorkflowRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowRun]:
        stmt = select(WorkflowRunRow).where(WorkflowRunRow.tenant_id == tenant_id)
        if workflow_id is not None:
            stmt = stmt.where(WorkflowRunRow.workflow_id == workflow_id)
        if status is not None:
            stmt = stmt.where(WorkflowRunRow.status == status.value)
        stmt = stmt.order_by(WorkflowRunRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_run_from_row(r) for r in rows]

    async def list_stale_runs(self, *, now: datetime, limit: int = 100) -> list[WorkflowRun]:
        stmt = (
            select(WorkflowRunRow)
            .where(
                WorkflowRunRow.status == WorkflowRunStatus.RUNNING.value,
                (WorkflowRunRow.lease_expires_at.is_(None))
                | (WorkflowRunRow.lease_expires_at <= now),
            )
            .order_by(WorkflowRunRow.updated_at)
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_run_from_row(r) for r in rows]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                update(WorkflowRunRow)
                .where(WorkflowRunRow.id == run_id, WorkflowRunRow.tenant_id == tenant_id)
                .values(worker_id=worker_id, lease_expires_at=lease_expires_at)
            )

    async def reset_running_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        stmt = (
            select(WorkflowStepRunRow)
            .where(
                WorkflowStepRunRow.tenant_id == tenant_id,
                WorkflowStepRunRow.workflow_run_id == workflow_run_id,
                WorkflowStepRunRow.status.in_(
                    [StepRunStatus.RUNNING.value, StepRunStatus.RETRYING.value]
                ),
            )
            .order_by(WorkflowStepRunRow.index)
        )
        reset: list[StepRun] = []
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                await session.execute(
                    update(WorkflowStepRunRow)
                    .where(
                        WorkflowStepRunRow.id == row.id,
                        WorkflowStepRunRow.tenant_id == tenant_id,
                        WorkflowStepRunRow.version == row.version,
                    )
                    .values(
                        status=StepRunStatus.PENDING.value,
                        updated_at=datetime.now(UTC),
                        finished_at=None,
                        version=row.version + 1,
                    )
                )
                refreshed = await session.get(WorkflowStepRunRow, row.id)
                if refreshed is not None:
                    reset.append(step_run_from_row(refreshed))
        return reset

    async def list_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        stmt = (
            select(WorkflowStepRunRow)
            .where(
                WorkflowStepRunRow.tenant_id == tenant_id,
                WorkflowStepRunRow.workflow_run_id == workflow_run_id,
            )
            .order_by(WorkflowStepRunRow.index)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [step_run_from_row(r) for r in rows]

    async def get_step(self, step_run_id: str, *, tenant_id: str) -> StepRun:
        async with self._db.transaction() as session:
            row = await session.get(WorkflowStepRunRow, step_run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("step run not found", details={"step_run_id": step_run_id})
        return step_run_from_row(row)

    async def update_step(self, step: StepRun, *, expected_version: int) -> StepRun:
        values = step_run_to_row(step)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(WorkflowStepRunRow)
                .where(
                    WorkflowStepRunRow.id == step.id,
                    WorkflowStepRunRow.tenant_id == step.tenant_id,
                    WorkflowStepRunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(WorkflowStepRunRow, step.id)
                if existing is None or existing.tenant_id != step.tenant_id:
                    raise NotFoundError("step run not found", details={"step_run_id": step.id})
                raise ConflictError(
                    "step run was modified concurrently",
                    details={"step_run_id": step.id, "expected": expected_version},
                )
            row = await session.get(WorkflowStepRunRow, step.id)
        assert row is not None
        return step_run_from_row(row)
