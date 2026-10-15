"""Run data contracts and the run state machine.

A *run* is the durable record of one user goal. It is the anchor that ties a
goal, its task plan, its approvals, its audit trail and its memory together, and
it is what makes an interrupted run recoverable: the worker re-reads the record
instead of relying on process memory.

Valid transitions are declared here and enforced on every mutation, so a stale
or duplicated worker cannot move a terminal run back into ``RUNNING``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import RunStatus
from ..core.errors import ConflictError
from ..core.ids import PREFIX_RUN, new_id
from ..specialists.base import GENERAL


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: Allowed state transitions. A run may only move along an edge in this graph.
RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.PLANNING, RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.PLANNING: frozenset(
        {
            RunStatus.RUNNING,
            RunStatus.WAITING_APPROVAL,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.RETRYING,
        }
    ),
    RunStatus.WAITING_APPROVAL: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED, RunStatus.FAILED}
    ),
    # RETRYING is entered when a stale RUNNING run is recovered; it must go back
    # through RUNNING (never straight to COMPLETED) so the work is actually done.
    RunStatus.RETRYING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset({RunStatus.RETRYING}),
    RunStatus.CANCELLED: frozenset(),
}

TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}
)


class RunRecord(BaseModel):
    """Durable state of one agent run."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_RUN))
    tenant_id: str
    user_id: str
    goal: str
    status: RunStatus = RunStatus.PENDING
    #: Absolute path of the sandboxed workspace for this run.
    workspace_dir: str = ""
    #: Optional caller-supplied key making run creation idempotent.
    idempotency_key: str | None = None
    #: Links a recovered/retried run to the run it continues.
    parent_run_id: str | None = None

    #: The specialist profile this run executes under (Phase 04). Stored so a
    #: worker reconstructs the same tool scope on resume/recovery without the
    #: caller resending it. Defaults to the general agent.
    specialist: str = GENERAL

    #: Principal identity needed to rebuild authorisation at execution time
    #: (roles and the permission ceiling). These are not secrets; storing them
    #: lets a worker reconstruct the principal without the caller's API key.
    roles: tuple[str, ...] = ()
    granted_permissions: tuple[str, ...] | None = None

    result: str = ""
    verified: bool = False
    error: str | None = None
    pending_approval_id: str | None = None

    steps_executed: int = 0
    tool_calls: int = 0
    retries: int = 0
    replans: int = 0
    #: How many times a worker has recovered this run after a crash/lease loss.
    recovery_attempts: int = 0
    usage: dict[str, int] = Field(default_factory=dict)

    #: Optimistic-concurrency version, incremented on every successful update.
    version: int = 1
    #: Worker that last claimed this run (for stale-worker detection).
    worker_id: str | None = None
    #: When the current worker lease expires (heartbeat anchor).
    lease_expires_at: datetime | None = None

    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATUSES

    @property
    def is_recoverable(self) -> bool:
        """Whether a run that stopped mid-flight may be resumed by a worker."""
        return self.status in {
            RunStatus.PENDING,
            RunStatus.PLANNING,
            RunStatus.RUNNING,
            RunStatus.RETRYING,
        }

    def can_transition_to(self, target: RunStatus) -> bool:
        return target in RUN_TRANSITIONS.get(self.status, frozenset())

    def transition(self, target: RunStatus, **updates: Any) -> RunRecord:
        """Return a copy moved to ``target``, or raise if the edge is invalid.

        ``updates`` may set any non-status field (result, error, counters, ...).
        """
        if target is self.status:
            # Idempotent re-application of the same state is allowed.
            return self.model_copy(update={**updates, "updated_at": _utcnow()})
        if not self.can_transition_to(target):
            raise ConflictError(
                f"invalid run transition {self.status.value} -> {target.value}",
                details={"run_id": self.id, "from": self.status.value, "to": target.value},
            )
        now = _utcnow()
        merged: dict[str, Any] = {"status": target, "updated_at": now, **updates}
        if target is RunStatus.RUNNING and self.started_at is None:
            merged["started_at"] = now
        if target in TERMINAL_RUN_STATUSES:
            merged["finished_at"] = now
        return self.model_copy(update=merged)
