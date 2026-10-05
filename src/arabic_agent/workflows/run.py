"""Workflow run and step run contracts, with enforced state machines.

A :class:`WorkflowRun` is the durable record of one execution of a
:class:`WorkflowVersion`. A :class:`StepRun` is the durable record of one node's
execution within that run. Both are persisted in Postgres and both use
optimistic concurrency (a ``version`` column) so two workers racing on the same
record cannot both advance it.

The transitions are declared here and enforced on every mutation. In particular
a terminal step can never be moved back to ``RUNNING`` — that is what stops a
redelivered job from re-executing a completed step (and, with the idempotency
guard, from repeating its external effect).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ErrorClass, StepKind, StepRunStatus, WorkflowRunStatus
from ..core.errors import ConflictError
from ..core.ids import PREFIX_STEP_RUN, PREFIX_WORKFLOW_RUN, new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Allowed transitions for a workflow run.
WORKFLOW_RUN_TRANSITIONS: dict[WorkflowRunStatus, frozenset[WorkflowRunStatus]] = {
    WorkflowRunStatus.PENDING: frozenset(
        {WorkflowRunStatus.RUNNING, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED}
    ),
    WorkflowRunStatus.RUNNING: frozenset(
        {
            WorkflowRunStatus.WAITING_APPROVAL,
            WorkflowRunStatus.COMPLETED,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
    ),
    WorkflowRunStatus.WAITING_APPROVAL: frozenset(
        {
            WorkflowRunStatus.RUNNING,
            WorkflowRunStatus.FAILED,
            WorkflowRunStatus.CANCELLED,
        }
    ),
    WorkflowRunStatus.COMPLETED: frozenset(),
    WorkflowRunStatus.FAILED: frozenset(),
    WorkflowRunStatus.CANCELLED: frozenset(),
}

TERMINAL_WORKFLOW_RUN_STATUSES: frozenset[WorkflowRunStatus] = frozenset(
    {WorkflowRunStatus.COMPLETED, WorkflowRunStatus.FAILED, WorkflowRunStatus.CANCELLED}
)

#: Allowed transitions for a step run.
STEP_RUN_TRANSITIONS: dict[StepRunStatus, frozenset[StepRunStatus]] = {
    StepRunStatus.PENDING: frozenset(
        {StepRunStatus.RUNNING, StepRunStatus.SKIPPED, StepRunStatus.CANCELLED}
    ),
    StepRunStatus.RUNNING: frozenset(
        {
            StepRunStatus.WAITING_APPROVAL,
            StepRunStatus.RETRYING,
            StepRunStatus.COMPLETED,
            StepRunStatus.FAILED,
            StepRunStatus.SKIPPED,
            StepRunStatus.CANCELLED,
        }
    ),
    StepRunStatus.WAITING_APPROVAL: frozenset(
        {
            StepRunStatus.RUNNING,
            StepRunStatus.COMPLETED,
            StepRunStatus.FAILED,
            StepRunStatus.SKIPPED,
            StepRunStatus.CANCELLED,
        }
    ),
    StepRunStatus.RETRYING: frozenset(
        {
            StepRunStatus.RUNNING,
            StepRunStatus.FAILED,
            StepRunStatus.SKIPPED,
            StepRunStatus.CANCELLED,
        }
    ),
    StepRunStatus.COMPLETED: frozenset(),
    StepRunStatus.FAILED: frozenset(),
    StepRunStatus.SKIPPED: frozenset(),
    StepRunStatus.CANCELLED: frozenset(),
}

TERMINAL_STEP_RUN_STATUSES: frozenset[StepRunStatus] = frozenset(
    {
        StepRunStatus.COMPLETED,
        StepRunStatus.FAILED,
        StepRunStatus.SKIPPED,
        StepRunStatus.CANCELLED,
    }
)

#: Step statuses that count as "successful" for dependency satisfaction.
SUCCESSFUL_STEP_RUN_STATUSES: frozenset[StepRunStatus] = frozenset(
    {StepRunStatus.COMPLETED, StepRunStatus.SKIPPED}
)


class WorkflowRun(BaseModel):
    """Durable state of one workflow execution."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_WORKFLOW_RUN))
    tenant_id: str
    user_id: str
    workflow_id: str
    workflow_version_id: str
    #: The definition version number pinned at start (never changes mid-run).
    workflow_version: int = 1
    status: WorkflowRunStatus = WorkflowRunStatus.PENDING
    #: Structured inputs supplied at start; available as ``input`` in templates.
    input: dict[str, Any] = Field(default_factory=dict)
    #: Structured outputs accumulated when the run completes.
    output: dict[str, Any] = Field(default_factory=dict)
    #: Sandboxed working directory shared by the run's tool/agent steps.
    workspace_dir: str = ""
    error: str | None = None
    pending_approval_id: str | None = None
    idempotency_key: str | None = None

    #: Wall-clock budget pinned from the version at start.
    run_timeout_seconds: float | None = None

    #: Principal identity, so a worker can rebuild authorisation at run time.
    roles: tuple[str, ...] = ()
    granted_permissions: tuple[str, ...] | None = None

    #: How many times a worker has recovered this run after a crash/lease loss.
    recovery_attempts: int = 0
    worker_id: str | None = None
    lease_expires_at: datetime | None = None

    version: int = 1
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_WORKFLOW_RUN_STATUSES

    @property
    def is_recoverable(self) -> bool:
        """Whether a run stopped mid-flight may be reclaimed by a worker."""
        return self.status in {WorkflowRunStatus.RUNNING}

    def can_transition_to(self, target: WorkflowRunStatus) -> bool:
        return target in WORKFLOW_RUN_TRANSITIONS.get(self.status, frozenset())

    def transition(self, target: WorkflowRunStatus, **updates: Any) -> WorkflowRun:
        if target is self.status:
            return self.model_copy(update={**updates, "updated_at": _utcnow()})
        if not self.can_transition_to(target):
            raise ConflictError(
                f"invalid workflow run transition {self.status.value} -> {target.value}",
                details={"workflow_run_id": self.id, "from": self.status.value, "to": target.value},
            )
        now = _utcnow()
        merged: dict[str, Any] = {"status": target, "updated_at": now, **updates}
        if target is WorkflowRunStatus.RUNNING and self.started_at is None:
            merged["started_at"] = now
        if target in TERMINAL_WORKFLOW_RUN_STATUSES:
            merged["finished_at"] = now
            merged["lease_expires_at"] = None
        return self.model_copy(update=merged)


class StepRun(BaseModel):
    """Durable state of one workflow step execution."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_STEP_RUN))
    tenant_id: str
    workflow_run_id: str
    #: The id of the step *in the workflow definition*.
    step_id: str
    index: int = 0
    name: str = ""
    kind: StepKind
    status: StepRunStatus = StepRunStatus.PENDING
    attempts: int = 0
    #: Structured output produced by the step, exposed to dependents as
    #: ``steps.<step_id>.output``.
    output: dict[str, Any] = Field(default_factory=dict)
    #: Short human-readable summary of the outcome.
    summary: str = ""
    error: str | None = None
    error_class: ErrorClass | None = None
    #: The agent run this step spawned, if it was an agent step.
    agent_run_id: str | None = None
    #: The approval this step is waiting on, if it was an approval/tool step.
    approval_id: str | None = None
    #: Resolved tool name, for tool steps.
    tool_name: str | None = None
    #: Idempotency key applied to this step's external effect.
    idempotency_key: str | None = None

    version: int = 1
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STEP_RUN_STATUSES

    @property
    def is_successful(self) -> bool:
        return self.status in SUCCESSFUL_STEP_RUN_STATUSES

    def can_transition_to(self, target: StepRunStatus) -> bool:
        return target in STEP_RUN_TRANSITIONS.get(self.status, frozenset())

    def transition(self, target: StepRunStatus, **updates: Any) -> StepRun:
        if target is self.status:
            return self.model_copy(update={**updates, "updated_at": _utcnow()})
        if not self.can_transition_to(target):
            raise ConflictError(
                f"invalid step run transition {self.status.value} -> {target.value}",
                details={"step_run_id": self.id, "from": self.status.value, "to": target.value},
            )
        now = _utcnow()
        merged: dict[str, Any] = {"status": target, "updated_at": now, **updates}
        if target is StepRunStatus.RUNNING and self.started_at is None:
            merged["started_at"] = now
        if target in TERMINAL_STEP_RUN_STATUSES:
            merged["finished_at"] = now
        return self.model_copy(update=merged)
