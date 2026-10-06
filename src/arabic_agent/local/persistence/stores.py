"""SQLite implementations of the existing persistence contracts.

Every class here implements an ABC the rest of the platform already depends on,
so the workflow engine, the tool executor, the permission layer and the sync
coordinator run unchanged against the local durable store:

* :class:`LocalWorkflowStore`, :class:`LocalWorkflowVersionStore` and
  :class:`LocalWorkflowRunStore` implement ``workflows.store``.
* :class:`LocalApprovalStore` implements ``permissions.approvals.ApprovalStore``.
* :class:`LocalAuditSink` implements ``audit.base.AuditSink``.
* :class:`LocalSyncQueue` implements ``local.sync.SyncQueue``.
* :class:`LocalIdempotencyGuard` implements ``infrastructure.idempotency``.

Row <-> domain conversion reuses ``persistence.mapping``: the local rows carry
the same column names as the cloud rows, so the same ``*_to_row`` /
``*_from_row`` helpers are used. That keeps a single definition of each domain
mapping and guarantees local and cloud stores agree on the contract.

Optimistic concurrency uses the same conditional
``UPDATE ... WHERE version = :n`` pattern as the Postgres stores, so a losing
writer gets a :class:`ConflictError` rather than silently overwriting.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from ...audit.base import AuditEvent, AuditSink
from ...connectors.ledger import ActionLedger, ActionRecord
from ...core.enums import (
    ActionState,
    ApprovalStatus,
    RunStatus,
    StepRunStatus,
    SyncStatus,
    WorkflowRunStatus,
)
from ...core.errors import ConflictError, NotFoundError
from ...core.redaction import redact
from ...infrastructure.idempotency import (
    IdempotencyGuard,
    IdempotencyOutcome,
    IdempotencyStatus,
)
from ...permissions.approvals import Approval, ApprovalStore
from ...persistence.mapping import (
    approval_from_row,
    approval_to_row,
    audit_from_row,
    audit_to_row,
    run_from_row,
    run_to_row,
    step_run_from_row,
    step_run_to_row,
    workflow_from_row,
    workflow_run_from_row,
    workflow_run_to_row,
    workflow_to_row,
    workflow_version_from_row,
    workflow_version_to_row,
)
from ...runs.base import RunRecord
from ...runs.store import RunStore
from ...workflows.models import Workflow, WorkflowVersion
from ...workflows.run import StepRun, WorkflowRun
from ...workflows.store import WorkflowRunStore, WorkflowStore, WorkflowVersionStore
from ..sync import SyncAction, SyncQueue
from .database import LocalDatabase
from .schema import (
    LocalActionLedgerRow,
    LocalApprovalRow,
    LocalAuditEventRow,
    LocalIdempotencyRow,
    LocalRunRow,
    LocalSyncActionRow,
    LocalWorkflowRow,
    LocalWorkflowRunRow,
    LocalWorkflowStepRunRow,
    LocalWorkflowVersionRow,
)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --- Workflows --------------------------------------------------------------
class LocalWorkflowStore(WorkflowStore):
    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def create(self, workflow: Workflow) -> Workflow:
        try:
            async with self._db.transaction() as session:
                session.add(LocalWorkflowRow(**workflow_to_row(workflow)))
        except IntegrityError as exc:
            raise ConflictError(
                "a workflow with this name already exists", details={"name": workflow.name}
            ) from exc
        return workflow

    async def get(self, workflow_id: str, *, tenant_id: str) -> Workflow:
        async with self._db.transaction() as session:
            row = await session.get(LocalWorkflowRow, workflow_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow not found", details={"workflow_id": workflow_id})
        return workflow_from_row(cast(Any, row))

    async def update(self, workflow: Workflow, *, expected_version: int) -> Workflow:
        values = workflow_to_row(workflow)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(LocalWorkflowRow)
                .where(
                    LocalWorkflowRow.id == workflow.id,
                    LocalWorkflowRow.tenant_id == workflow.tenant_id,
                    LocalWorkflowRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(LocalWorkflowRow, workflow.id)
                if existing is None or existing.tenant_id != workflow.tenant_id:
                    raise NotFoundError("workflow not found", details={"workflow_id": workflow.id})
                raise ConflictError(
                    "workflow was modified concurrently",
                    details={"workflow_id": workflow.id, "expected": expected_version},
                )
            row = await session.get(LocalWorkflowRow, workflow.id)
        assert row is not None
        return workflow_from_row(cast(Any, row))

    async def list_workflows(
        self, *, tenant_id: str, limit: int = 50, offset: int = 0
    ) -> list[Workflow]:
        stmt = (
            select(LocalWorkflowRow)
            .where(LocalWorkflowRow.tenant_id == tenant_id)
            .order_by(LocalWorkflowRow.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_from_row(cast(Any, r)) for r in rows]


class LocalWorkflowVersionStore(WorkflowVersionStore):
    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def create(self, version: WorkflowVersion) -> WorkflowVersion:
        try:
            async with self._db.transaction() as session:
                session.add(LocalWorkflowVersionRow(**workflow_version_to_row(version)))
        except IntegrityError as exc:
            raise ConflictError(
                "workflow version already exists", details={"version_id": version.id}
            ) from exc
        return version

    async def get(self, version_id: str, *, tenant_id: str) -> WorkflowVersion:
        async with self._db.transaction() as session:
            row = await session.get(LocalWorkflowVersionRow, version_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow version not found", details={"version_id": version_id})
        return workflow_version_from_row(cast(Any, row))

    async def latest(self, *, tenant_id: str, workflow_id: str) -> WorkflowVersion | None:
        stmt = (
            select(LocalWorkflowVersionRow)
            .where(
                LocalWorkflowVersionRow.tenant_id == tenant_id,
                LocalWorkflowVersionRow.workflow_id == workflow_id,
            )
            .order_by(LocalWorkflowVersionRow.version.desc())
            .limit(1)
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return workflow_version_from_row(cast(Any, row)) if row else None

    async def list_versions(self, *, tenant_id: str, workflow_id: str) -> list[WorkflowVersion]:
        stmt = (
            select(LocalWorkflowVersionRow)
            .where(
                LocalWorkflowVersionRow.tenant_id == tenant_id,
                LocalWorkflowVersionRow.workflow_id == workflow_id,
            )
            .order_by(LocalWorkflowVersionRow.version.desc())
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_version_from_row(cast(Any, r)) for r in rows]


class LocalWorkflowRunStore(WorkflowRunStore):
    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def create_run(self, run: WorkflowRun, steps: list[StepRun]) -> WorkflowRun:
        try:
            async with self._db.transaction() as session:
                session.add(LocalWorkflowRunRow(**workflow_run_to_row(run)))
                for step in steps:
                    session.add(LocalWorkflowStepRunRow(**step_run_to_row(step)))
        except IntegrityError as exc:
            raise ConflictError(
                "workflow run with this idempotency key already exists",
                details={"idempotency_key": run.idempotency_key},
            ) from exc
        return run

    async def get_run(self, run_id: str, *, tenant_id: str) -> WorkflowRun:
        async with self._db.transaction() as session:
            row = await session.get(LocalWorkflowRunRow, run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("workflow run not found", details={"workflow_run_id": run_id})
        return workflow_run_from_row(cast(Any, row))

    async def get_run_by_idempotency_key(self, *, tenant_id: str, key: str) -> WorkflowRun | None:
        stmt = select(LocalWorkflowRunRow).where(
            LocalWorkflowRunRow.tenant_id == tenant_id,
            LocalWorkflowRunRow.idempotency_key == key,
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return workflow_run_from_row(cast(Any, row)) if row else None

    async def update_run(self, run: WorkflowRun, *, expected_version: int) -> WorkflowRun:
        values = workflow_run_to_row(run)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(LocalWorkflowRunRow)
                .where(
                    LocalWorkflowRunRow.id == run.id,
                    LocalWorkflowRunRow.tenant_id == run.tenant_id,
                    LocalWorkflowRunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(LocalWorkflowRunRow, run.id)
                if existing is None or existing.tenant_id != run.tenant_id:
                    raise NotFoundError(
                        "workflow run not found", details={"workflow_run_id": run.id}
                    )
                raise ConflictError(
                    "workflow run was modified concurrently",
                    details={"workflow_run_id": run.id, "expected": expected_version},
                )
            row = await session.get(LocalWorkflowRunRow, run.id)
        assert row is not None
        return workflow_run_from_row(cast(Any, row))

    async def list_runs(
        self,
        *,
        tenant_id: str,
        workflow_id: str | None = None,
        status: WorkflowRunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WorkflowRun]:
        stmt = select(LocalWorkflowRunRow).where(LocalWorkflowRunRow.tenant_id == tenant_id)
        if workflow_id is not None:
            stmt = stmt.where(LocalWorkflowRunRow.workflow_id == workflow_id)
        if status is not None:
            stmt = stmt.where(LocalWorkflowRunRow.status == status.value)
        stmt = stmt.order_by(LocalWorkflowRunRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_run_from_row(cast(Any, r)) for r in rows]

    async def list_stale_runs(self, *, now: datetime, limit: int = 100) -> list[WorkflowRun]:
        stmt = (
            select(LocalWorkflowRunRow)
            .where(
                LocalWorkflowRunRow.status == WorkflowRunStatus.RUNNING.value,
                (LocalWorkflowRunRow.lease_expires_at.is_(None))
                | (LocalWorkflowRunRow.lease_expires_at <= now),
            )
            .order_by(LocalWorkflowRunRow.updated_at)
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [workflow_run_from_row(cast(Any, r)) for r in rows]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                update(LocalWorkflowRunRow)
                .where(
                    LocalWorkflowRunRow.id == run_id,
                    LocalWorkflowRunRow.tenant_id == tenant_id,
                )
                .values(worker_id=worker_id, lease_expires_at=lease_expires_at)
            )

    async def reset_running_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        stmt = select(LocalWorkflowStepRunRow).where(
            LocalWorkflowStepRunRow.tenant_id == tenant_id,
            LocalWorkflowStepRunRow.workflow_run_id == workflow_run_id,
            LocalWorkflowStepRunRow.status.in_(
                [StepRunStatus.RUNNING.value, StepRunStatus.RETRYING.value]
            ),
        )
        reset: list[StepRun] = []
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
            for row in rows:
                await session.execute(
                    update(LocalWorkflowStepRunRow)
                    .where(LocalWorkflowStepRunRow.id == row.id)
                    .values(
                        status=StepRunStatus.PENDING.value,
                        updated_at=_utcnow(),
                        finished_at=None,
                        version=row.version + 1,
                    )
                )
            for row in rows:
                refreshed = await session.get(LocalWorkflowStepRunRow, row.id)
                if refreshed is not None:
                    reset.append(step_run_from_row(cast(Any, refreshed)))
        return reset

    async def list_steps(self, *, tenant_id: str, workflow_run_id: str) -> list[StepRun]:
        stmt = (
            select(LocalWorkflowStepRunRow)
            .where(
                LocalWorkflowStepRunRow.tenant_id == tenant_id,
                LocalWorkflowStepRunRow.workflow_run_id == workflow_run_id,
            )
            .order_by(LocalWorkflowStepRunRow.index)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [step_run_from_row(cast(Any, r)) for r in rows]

    async def get_step(self, step_run_id: str, *, tenant_id: str) -> StepRun:
        async with self._db.transaction() as session:
            row = await session.get(LocalWorkflowStepRunRow, step_run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("step run not found", details={"step_run_id": step_run_id})
        return step_run_from_row(cast(Any, row))

    async def update_step(self, step: StepRun, *, expected_version: int) -> StepRun:
        values = step_run_to_row(step)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(LocalWorkflowStepRunRow)
                .where(
                    LocalWorkflowStepRunRow.id == step.id,
                    LocalWorkflowStepRunRow.tenant_id == step.tenant_id,
                    LocalWorkflowStepRunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(LocalWorkflowStepRunRow, step.id)
                if existing is None or existing.tenant_id != step.tenant_id:
                    raise NotFoundError("step run not found", details={"step_run_id": step.id})
                raise ConflictError(
                    "step run was modified concurrently",
                    details={"step_run_id": step.id, "expected": expected_version},
                )
            row = await session.get(LocalWorkflowStepRunRow, step.id)
        assert row is not None
        return step_run_from_row(cast(Any, row))


# --- Approvals --------------------------------------------------------------
class LocalApprovalStore(ApprovalStore):
    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def add(self, approval: Approval) -> Approval:
        try:
            async with self._db.transaction() as session:
                session.add(LocalApprovalRow(**approval_to_row(approval)))
        except IntegrityError as exc:
            raise ConflictError(
                "approval already exists", details={"approval_id": approval.id}
            ) from exc
        return approval

    async def get(self, approval_id: str, *, tenant_id: str) -> Approval:
        async with self._db.transaction() as session:
            row = await session.get(LocalApprovalRow, approval_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("approval not found", details={"approval_id": approval_id})
        return approval_from_row(cast(Any, row))

    async def resolve(
        self,
        approval_id: str,
        *,
        tenant_id: str,
        status: ApprovalStatus,
        resolved_by: str,
        reason: str | None = None,
    ) -> Approval:
        async with self._db.transaction() as session:
            row = await session.get(LocalApprovalRow, approval_id)
            if row is None or row.tenant_id != tenant_id:
                raise NotFoundError("approval not found", details={"approval_id": approval_id})
            if ApprovalStatus(row.status) is not ApprovalStatus.PENDING:
                raise ConflictError(
                    "approval has already been resolved",
                    details={"approval_id": approval_id, "status": row.status},
                )
            await session.execute(
                update(LocalApprovalRow)
                .where(LocalApprovalRow.id == approval_id)
                .values(
                    status=status.value,
                    resolved_by=resolved_by,
                    resolved_at=_utcnow(),
                    decision_reason=reason,
                )
            )
            refreshed = await session.get(LocalApprovalRow, approval_id)
        assert refreshed is not None
        return approval_from_row(cast(Any, refreshed))

    async def list_pending(self, *, tenant_id: str) -> list[Approval]:
        stmt = select(LocalApprovalRow).where(
            LocalApprovalRow.tenant_id == tenant_id,
            LocalApprovalRow.status == ApprovalStatus.PENDING.value,
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [approval_from_row(cast(Any, r)) for r in rows]

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[Approval]:
        stmt = select(LocalApprovalRow).where(
            LocalApprovalRow.tenant_id == tenant_id,
            LocalApprovalRow.run_id == run_id,
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [approval_from_row(cast(Any, r)) for r in rows]


# --- Audit ------------------------------------------------------------------
class LocalAuditSink(AuditSink):
    """Durable local audit sink.

    Events are redacted before they reach a sink (the event constructor redacts
    payloads), so a secret can never be persisted here. The sequence is derived
    from the durable maximum for the run, mirroring the Postgres sink, so a
    restarted process never collides on ``(run_id, sequence)``.
    """

    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def next_sequence(self, *, run_id: str) -> int:
        async with self._db.transaction() as session:
            highest = (
                await session.execute(
                    select(func.max(LocalAuditEventRow.sequence)).where(
                        LocalAuditEventRow.run_id == run_id
                    )
                )
            ).scalar_one_or_none()
        return int(highest or 0) + 1

    async def emit(self, event: AuditEvent) -> None:
        async with self._db.transaction() as session:
            session.add(LocalAuditEventRow(**audit_to_row(event)))

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        stmt = (
            select(LocalAuditEventRow)
            .where(
                LocalAuditEventRow.tenant_id == tenant_id,
                LocalAuditEventRow.run_id == run_id,
            )
            .order_by(LocalAuditEventRow.timestamp, LocalAuditEventRow.sequence)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [audit_from_row(cast(Any, r)) for r in rows]


# --- Sync queue -------------------------------------------------------------
def _sync_to_row(action: SyncAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "tenant_id": action.tenant_id,
        "user_id": action.user_id,
        "device_id": action.device_id,
        "kind": action.kind.value,
        "payload": dict(action.payload),
        "status": action.status.value,
        "attempts": action.attempts,
        "error": action.error,
        "created_at": action.created_at,
        "updated_at": action.updated_at,
    }


def _sync_from_row(row: LocalSyncActionRow) -> SyncAction:
    from ...core.enums import SyncKind

    return SyncAction(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        device_id=row.device_id,
        kind=SyncKind(row.kind),
        payload=dict(row.payload or {}),
        status=SyncStatus(row.status),
        attempts=row.attempts,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class LocalSyncQueue(SyncQueue):
    """Durable local sync queue. Survives a restart while offline."""

    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def add(self, action: SyncAction) -> SyncAction:
        async with self._db.transaction() as session:
            session.add(LocalSyncActionRow(**_sync_to_row(action)))
        return action

    async def get(self, action_id: str, *, tenant_id: str) -> SyncAction:
        async with self._db.transaction() as session:
            row = await session.get(LocalSyncActionRow, action_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("sync action not found", details={"action_id": action_id})
        return _sync_from_row(row)

    async def list_pending(self, *, tenant_id: str) -> list[SyncAction]:
        stmt = (
            select(LocalSyncActionRow)
            .where(
                LocalSyncActionRow.tenant_id == tenant_id,
                LocalSyncActionRow.status == SyncStatus.PENDING.value,
            )
            .order_by(LocalSyncActionRow.created_at)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_sync_from_row(r) for r in rows]

    async def list_all(self, *, tenant_id: str) -> list[SyncAction]:
        stmt = (
            select(LocalSyncActionRow)
            .where(LocalSyncActionRow.tenant_id == tenant_id)
            .order_by(LocalSyncActionRow.created_at)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_sync_from_row(r) for r in rows]

    async def update(self, action: SyncAction) -> SyncAction:
        values = _sync_to_row(action)
        values.pop("id")
        async with self._db.transaction() as session:
            row = await session.get(LocalSyncActionRow, action.id)
            if row is None:
                raise NotFoundError("sync action not found", details={"action_id": action.id})
            await session.execute(
                update(LocalSyncActionRow)
                .where(LocalSyncActionRow.id == action.id)
                .values(**values)
            )
        return action


# --- Idempotency ------------------------------------------------------------
class LocalIdempotencyGuard(IdempotencyGuard):
    """Durable local idempotency guard.

    Persists the claim and the completed result, so a side effect is not
    repeated across a process restart (unlike the in-memory guard). The stored
    result is redacted. Expired rows are treated as absent.
    """

    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def begin(self, *, key: str, fingerprint: str, ttl_seconds: int) -> IdempotencyStatus:
        now = _utcnow()
        async with self._db.transaction() as session:
            row = await session.get(LocalIdempotencyRow, key)
            if row is not None and row.expires_at <= now:
                await session.execute(
                    delete(LocalIdempotencyRow).where(LocalIdempotencyRow.key == key)
                )
                row = None
            if row is None:
                session.add(
                    LocalIdempotencyRow(
                        key=key,
                        state=f"in_flight:{fingerprint}",
                        expires_at=now + timedelta(seconds=ttl_seconds),
                        updated_at=now,
                    )
                )
                return IdempotencyStatus(outcome=IdempotencyOutcome.ACQUIRED)
            state = row.state
        if state.startswith("done:"):
            return IdempotencyStatus(
                outcome=IdempotencyOutcome.COMPLETED,
                result=json.loads(state[len("done:") :]),
            )
        return IdempotencyStatus(outcome=IdempotencyOutcome.IN_FLIGHT)

    async def complete(self, *, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        now = _utcnow()
        payload = json.dumps(redact(result), ensure_ascii=False, default=str)
        async with self._db.transaction() as session:
            row = await session.get(LocalIdempotencyRow, key)
            if row is None:
                session.add(
                    LocalIdempotencyRow(
                        key=key,
                        state=f"done:{payload}",
                        expires_at=now + timedelta(seconds=ttl_seconds),
                        updated_at=now,
                    )
                )
            else:
                await session.execute(
                    update(LocalIdempotencyRow)
                    .where(LocalIdempotencyRow.key == key)
                    .values(
                        state=f"done:{payload}",
                        expires_at=now + timedelta(seconds=ttl_seconds),
                        updated_at=now,
                    )
                )

    async def abort(self, *, key: str) -> None:
        async with self._db.transaction() as session:
            await session.execute(delete(LocalIdempotencyRow).where(LocalIdempotencyRow.key == key))


# --- Durable local agent runs (desktop sessions/tasks) ----------------------
class LocalRunStore(RunStore):
    """Durable local run store: the desktop runtime's sessions survive a restart.

    Implements the same :class:`~arabic_agent.runs.store.RunStore` contract as
    the Postgres store (optimistic concurrency included), so the API service,
    the worker and the recovery coordinator run unchanged on the local store.
    """

    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def create(self, record: RunRecord) -> RunRecord:
        try:
            async with self._db.transaction() as session:
                session.add(LocalRunRow(**run_to_row(record)))
        except IntegrityError as exc:
            raise ConflictError(
                "run with this idempotency key already exists",
                details={"idempotency_key": record.idempotency_key},
            ) from exc
        return record

    async def get(self, run_id: str, *, tenant_id: str) -> RunRecord:
        async with self._db.transaction() as session:
            row = await session.get(LocalRunRow, run_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("run not found", details={"run_id": run_id})
        return run_from_row(cast(Any, row))

    async def get_by_idempotency_key(self, *, tenant_id: str, key: str) -> RunRecord | None:
        stmt = select(LocalRunRow).where(
            LocalRunRow.tenant_id == tenant_id,
            LocalRunRow.idempotency_key == key,
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return run_from_row(cast(Any, row)) if row else None

    async def update(self, record: RunRecord, *, expected_version: int) -> RunRecord:
        values = run_to_row(record)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(LocalRunRow)
                .where(
                    LocalRunRow.id == record.id,
                    LocalRunRow.tenant_id == record.tenant_id,
                    LocalRunRow.version == expected_version,
                )
                .values(**values, version=expected_version + 1)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                existing = await session.get(LocalRunRow, record.id)
                if existing is None or existing.tenant_id != record.tenant_id:
                    raise NotFoundError("run not found", details={"run_id": record.id})
                raise ConflictError(
                    "run was modified concurrently",
                    details={"run_id": record.id, "expected": expected_version},
                )
            row = await session.get(LocalRunRow, record.id)
        assert row is not None
        return run_from_row(cast(Any, row))

    async def list_runs(
        self,
        *,
        tenant_id: str,
        status: RunStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        stmt = select(LocalRunRow).where(LocalRunRow.tenant_id == tenant_id)
        if status is not None:
            stmt = stmt.where(LocalRunRow.status == status.value)
        stmt = stmt.order_by(LocalRunRow.created_at.desc()).limit(limit).offset(offset)
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [run_from_row(cast(Any, r)) for r in rows]

    async def list_stale(self, *, now: datetime, limit: int = 100) -> list[RunRecord]:
        recoverable = tuple(
            s.value for s in (RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.RETRYING)
        )
        stmt = (
            select(LocalRunRow)
            .where(
                LocalRunRow.status.in_(recoverable),
                (LocalRunRow.lease_expires_at.is_(None)) | (LocalRunRow.lease_expires_at <= now),
            )
            .order_by(LocalRunRow.updated_at)
            .limit(limit)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [run_from_row(cast(Any, r)) for r in rows]

    async def heartbeat(
        self, run_id: str, *, tenant_id: str, worker_id: str, lease_expires_at: datetime
    ) -> None:
        async with self._db.transaction() as session:
            await session.execute(
                update(LocalRunRow)
                .where(LocalRunRow.id == run_id, LocalRunRow.tenant_id == tenant_id)
                .values(worker_id=worker_id, lease_expires_at=lease_expires_at)
            )


# --- Durable local external-action ledger -----------------------------------
class LocalActionLedger(ActionLedger):
    """Durable local action ledger (survives an application/runtime restart).

    The in-memory ledger cannot honour the platform's core promise — that a
    consequential external effect is never repeated after a crash — because its
    deduplication state is lost with the process. This store persists the same
    :class:`~arabic_agent.connectors.ledger.ActionRecord` in SQLite, so the
    idempotency key, approval binding and ambiguous-timeout (``UNKNOWN``) state
    all survive a restart. Only safe metadata and a canonical digest are stored;
    never the raw payload or a secret.
    """

    def __init__(self, db: LocalDatabase) -> None:
        self._db = db

    async def add(self, record: ActionRecord) -> ActionRecord:
        try:
            async with self._db.transaction() as session:
                session.add(LocalActionLedgerRow(**_action_to_row(record)))
        except IntegrityError as exc:
            raise ConflictError(
                "an action with this idempotency key already exists",
                details={"idempotency_key": record.idempotency_key},
            ) from exc
        return record

    async def get(self, action_id: str, *, tenant_id: str) -> ActionRecord:
        async with self._db.transaction() as session:
            row = await session.get(LocalActionLedgerRow, action_id)
        if row is None or row.tenant_id != tenant_id:
            raise NotFoundError("action not found", details={"action_id": action_id})
        return _action_from_row(row)

    async def update(self, record: ActionRecord) -> ActionRecord:
        values = _action_to_row(record)
        async with self._db.transaction() as session:
            result = await session.execute(
                update(LocalActionLedgerRow)
                .where(
                    LocalActionLedgerRow.id == record.id,
                    LocalActionLedgerRow.tenant_id == record.tenant_id,
                )
                .values(**values)
            )
            if cast(CursorResult[Any], result).rowcount == 0:
                raise NotFoundError("action not found", details={"action_id": record.id})
        return record

    async def find_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ActionRecord | None:
        stmt = select(LocalActionLedgerRow).where(
            LocalActionLedgerRow.tenant_id == tenant_id,
            LocalActionLedgerRow.idempotency_key == idempotency_key,
        )
        async with self._db.transaction() as session:
            row = (await session.execute(stmt)).scalar_one_or_none()
        return _action_from_row(row) if row else None

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[ActionRecord]:
        stmt = (
            select(LocalActionLedgerRow)
            .where(
                LocalActionLedgerRow.tenant_id == tenant_id,
                LocalActionLedgerRow.run_id == run_id,
            )
            .order_by(LocalActionLedgerRow.created_at)
        )
        async with self._db.transaction() as session:
            rows = (await session.execute(stmt)).scalars().all()
        return [_action_from_row(r) for r in rows]

    async def prune_before(self, *, cutoff: datetime) -> int:
        """Delete terminal ledger rows older than ``cutoff`` (bounded retention).

        Only terminal records (``VERIFIED``/``CANCELLED``) are removed; an
        unresolved ``UNKNOWN`` row is retained so its ambiguous effect is never
        forgotten. Returns the number of rows removed.
        """
        terminal = (ActionState.VERIFIED.value, ActionState.CANCELLED.value)
        async with self._db.transaction() as session:
            result = await session.execute(
                delete(LocalActionLedgerRow).where(
                    LocalActionLedgerRow.state.in_(terminal),
                    LocalActionLedgerRow.updated_at < cutoff,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)


def _action_to_row(record: ActionRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "run_id": record.run_id,
        "step_id": record.step_id,
        "connector": record.connector,
        "operation": record.operation,
        "risk": record.risk,
        "state": record.state.value,
        "request_digest": record.request_digest,
        "idempotency_key": record.idempotency_key,
        "approval_id": record.approval_id,
        "approved_digest": record.approved_digest,
        "attempts": record.attempts,
        "provider_request_id": record.provider_request_id,
        "verification_status": record.verification_status,
        "error": record.error,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def _action_from_row(row: LocalActionLedgerRow) -> ActionRecord:
    return ActionRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        run_id=row.run_id,
        step_id=row.step_id,
        connector=row.connector,
        operation=row.operation,
        risk=row.risk,
        state=ActionState(row.state),
        request_digest=row.request_digest,
        idempotency_key=row.idempotency_key,
        approval_id=row.approval_id,
        approved_digest=row.approved_digest,
        attempts=row.attempts,
        provider_request_id=row.provider_request_id,
        verification_status=row.verification_status,
        error=row.error,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


__all__ = [
    "LocalActionLedger",
    "LocalApprovalStore",
    "LocalAuditSink",
    "LocalIdempotencyGuard",
    "LocalRunStore",
    "LocalSyncQueue",
    "LocalWorkflowRunStore",
    "LocalWorkflowStore",
    "LocalWorkflowVersionStore",
]
