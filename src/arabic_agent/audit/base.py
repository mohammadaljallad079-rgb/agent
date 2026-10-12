"""Audit event model and sink contract.

Every meaningful step — planning, tool call, result, error, retry, approval,
final result — is recorded. Payloads are redacted on construction so secrets
can never reach a sink, even if a caller forgets.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import StrEnum
from ..core.ids import PREFIX_RUN, new_id
from ..core.redaction import redact


class AuditEventType(StrEnum):
    RUN_STARTED = "run.started"
    RUN_RECOVERED = "run.recovered"
    RUN_RETRIED = "run.retried"
    PLAN_CREATED = "plan.created"
    PLAN_REVISED = "plan.revised"
    STEP_STARTED = "step.started"
    STEP_COMPLETED = "step.completed"
    STEP_FAILED = "step.failed"
    TOOL_CALLED = "tool.called"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"
    TOOL_RETRY = "tool.retry"
    POLICY_DECISION = "policy.decision"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_RESOLVED = "approval.resolved"
    MODEL_CALL = "model.call"
    MODEL_ACTIVATED = "model.activated"
    MODEL_DEACTIVATED = "model.deactivated"
    MEMORY_WRITE = "memory.write"
    MEMORY_READ = "memory.read"
    JOB_ENQUEUED = "job.enqueued"
    JOB_CLAIMED = "job.claimed"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    AUTH_SUCCESS = "auth.success"
    AUTH_FAILURE = "auth.failure"
    SECURITY_EVENT = "security.event"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    # Workflow engine
    WORKFLOW_CREATED = "workflow.created"
    WORKFLOW_UPDATED = "workflow.updated"
    WORKFLOW_PUBLISHED = "workflow.published"
    WORKFLOW_RUN_STARTED = "workflow.run.started"
    WORKFLOW_STEP_STARTED = "workflow.step.started"
    WORKFLOW_STEP_COMPLETED = "workflow.step.completed"
    WORKFLOW_STEP_FAILED = "workflow.step.failed"
    WORKFLOW_STEP_RETRY = "workflow.step.retry"
    WORKFLOW_STEP_SKIPPED = "workflow.step.skipped"
    WORKFLOW_APPROVAL_REQUESTED = "workflow.approval.requested"
    WORKFLOW_APPROVAL_RESOLVED = "workflow.approval.resolved"
    WORKFLOW_RUN_COMPLETED = "workflow.run.completed"
    WORKFLOW_RUN_FAILED = "workflow.run.failed"
    WORKFLOW_RUN_CANCELLED = "workflow.run.cancelled"
    # Triggers & scheduler
    TRIGGER_CREATED = "trigger.created"
    TRIGGER_UPDATED = "trigger.updated"
    TRIGGER_ENABLED = "trigger.enabled"
    TRIGGER_DISABLED = "trigger.disabled"
    TRIGGER_DELETED = "trigger.deleted"
    TRIGGER_FIRED = "trigger.fired"
    TRIGGER_FIRE_SKIPPED = "trigger.fire.skipped"
    TRIGGER_FIRE_FAILED = "trigger.fire.failed"
    SCHEDULED_RUN_CREATED = "trigger.scheduled.run_created"
    WEBHOOK_RECEIVED = "trigger.webhook.received"
    WEBHOOK_REJECTED = "trigger.webhook.rejected"
    EVENT_RECEIVED = "trigger.event.received"
    RECOVERY_STARTED = "recovery.started"
    RECOVERY_SUCCEEDED = "recovery.succeeded"
    RECOVERY_REJECTED = "recovery.rejected"
    RECOVERY_LEASE_ACQUIRED = "recovery.lease.acquired"
    RECOVERY_LEASE_CONFLICT = "recovery.lease.conflict"
    WORKFLOW_RESUMED = "workflow.run.resumed"
    LOOP_DETECTED = "agent.loop.detected"
    DOCUMENT_PARSED = "document.parsed"
    DOCUMENT_RETRIEVED = "document.retrieved"
    DOCUMENT_GENERATED = "document.generated"
    PROJECT_INSPECTED = "project.inspected"
    ARTIFACT_OPENED = "artifact.opened"
    SECRET_DETECTED = "security.secret_detected"  # noqa: S105 - event name, not a credential
    # Local / desktop runtime
    LOCAL_RUNTIME_STARTED = "local.runtime.started"
    LOCAL_RUNTIME_READY = "local.runtime.ready"
    LOCAL_RUNTIME_PAUSED = "local.runtime.paused"
    LOCAL_RUNTIME_RESUMED = "local.runtime.resumed"
    LOCAL_RUNTIME_STOPPED = "local.runtime.stopped"
    LOCAL_RUNTIME_ERROR = "local.runtime.error"
    LOCAL_TOOL_REQUESTED = "local.tool.requested"
    LOCAL_FILE_ACCESSED = "local.file.accessed"
    LOCAL_FILE_MODIFIED = "local.file.modified"
    LOCAL_COMMAND_ATTEMPTED = "local.command.attempted"
    LOCAL_NETWORK_ATTEMPTED = "local.network.attempted"
    LOCAL_APPROVAL_REQUESTED = "local.approval.requested"
    LOCAL_APPROVAL_RESOLVED = "local.approval.resolved"
    LOCAL_DATA_BOUNDARY_DECISION = "local.data_boundary.decision"
    LOCAL_PERMISSION_GRANTED = "local.permission.granted"
    LOCAL_PERMISSION_DENIED = "local.permission.denied"
    DEVICE_ENROLLED = "device.enrolled"
    DEVICE_ACTIVATED = "device.activated"
    DEVICE_SUSPENDED = "device.suspended"
    DEVICE_REVOKED = "device.revoked"
    SYNC_QUEUED = "sync.queued"
    SYNC_UPLOADED = "sync.uploaded"
    SYNC_REJECTED = "sync.rejected"
    SYNC_FAILED = "sync.failed"
    # Computer use / perception (Phase 7). These record *metadata* only — an
    # observation id, dimensions, window and policy — never a raw screenshot or
    # typed secret value.
    COMPUTER_SCREEN_CAPTURED = "computer.screen_captured"
    COMPUTER_POINTER_MOVED = "computer.pointer_moved"
    COMPUTER_CLICKED = "computer.clicked"
    COMPUTER_SCROLLED = "computer.scrolled"
    COMPUTER_TEXT_TYPED = "computer.text_typed"
    COMPUTER_KEY_PRESSED = "computer.key_pressed"
    COMPUTER_WINDOW_FOCUSED = "computer.window_focused"
    COMPUTER_ACTION_VERIFIED = "computer.action_verified"
    COMPUTER_ACTION_FAILED = "computer.action_failed"
    COMPUTER_STOP_REQUESTED = "computer.stop_requested"
    COMPUTER_ACTION_THROTTLED = "computer.action_throttled"
    COMPUTER_OBSERVATION_STALE = "computer.observation_stale"
    COMPUTER_BOUNDARY_DECISION = "computer.boundary_decision"
    # Printing (Phase 11). These record *metadata* only — a printer id, job id,
    # document name, page count and policy decision — never the document bytes.
    PRINT_PRINTERS_LISTED = "print.printers_listed"
    PRINT_JOB_SUBMITTED = "print.job_submitted"
    PRINT_JOB_FAILED = "print.job_failed"
    PRINT_JOB_CANCELLED = "print.job_cancelled"
    PRINT_BOUNDARY_DECISION = "print.boundary_decision"
    # Browser / web (Phase 8). These record *metadata* only — a URL, title,
    # element counts, action kind and policy decision — never the raw page text,
    # a typed secret value, or a screenshot.
    BROWSER_OBSERVED = "browser.observed"
    BROWSER_NAVIGATED = "browser.navigated"
    BROWSER_NAVIGATION_BLOCKED = "browser.navigation_blocked"
    BROWSER_INTERACTED = "browser.interacted"
    BROWSER_FORM_SUBMITTED = "browser.form_submitted"
    BROWSER_DOWNLOADED = "browser.downloaded"
    BROWSER_UPLOADED = "browser.uploaded"
    BROWSER_TAB_OPENED = "browser.tab_opened"
    BROWSER_TAB_SWITCHED = "browser.tab_switched"
    BROWSER_TAB_CLOSED = "browser.tab_closed"
    BROWSER_DIALOG_RESOLVED = "browser.dialog_resolved"
    BROWSER_WAITED = "browser.waited"
    BROWSER_ACTION_VERIFIED = "browser.action_verified"
    BROWSER_ACTION_FAILED = "browser.action_failed"
    BROWSER_STOP_REQUESTED = "browser.stop_requested"
    BROWSER_BOUNDARY_DECISION = "browser.boundary_decision"
    BROWSER_INJECTION_SUSPECTED = "browser.injection_suspected"
    BROWSER_RESEARCH_SOURCE = "browser.research_source"
    # External connectors & real-world actions (Phase 9). These record connector
    # metadata, operation, risk, approval binding, retries, verification and the
    # action-ledger state — never a raw secret, message body or record payload.
    CONNECTOR_REGISTERED = "connector.registered"
    CONNECTOR_CALLED = "connector.called"
    CONNECTOR_RESULT = "connector.result"
    CONNECTOR_ERROR = "connector.error"
    CONNECTOR_HEALTHCHECK = "connector.healthcheck"
    CONNECTOR_UNAVAILABLE = "connector.unavailable"
    CREDENTIAL_RESOLVED = "credential.resolved"
    CREDENTIAL_MISSING = "credential.missing"
    OAUTH_AUTHORIZATION_REQUESTED = "oauth.authorization_requested"
    OAUTH_CALLBACK_ACCEPTED = "oauth.callback_accepted"
    OAUTH_CALLBACK_REJECTED = "oauth.callback_rejected"
    OAUTH_TOKEN_REFRESHED = "oauth.token_refreshed"  # noqa: S105 - event name, not a credential
    OAUTH_REVOKED = "oauth.revoked"
    OAUTH_REAUTH_REQUIRED = "oauth.reauth_required"
    EXTERNAL_ACTION_PREPARED = "external_action.prepared"
    EXTERNAL_ACTION_APPROVAL_BOUND = "external_action.approval_bound"
    EXTERNAL_ACTION_APPROVAL_MISMATCH = "external_action.approval_mismatch"
    EXTERNAL_ACTION_EXECUTING = "external_action.executing"
    EXTERNAL_ACTION_SUCCEEDED = "external_action.succeeded"
    EXTERNAL_ACTION_FAILED = "external_action.failed"
    EXTERNAL_ACTION_UNKNOWN = "external_action.unknown"
    EXTERNAL_ACTION_VERIFYING = "external_action.verifying"
    EXTERNAL_ACTION_VERIFIED = "external_action.verified"
    EXTERNAL_ACTION_CANCELLED = "external_action.cancelled"
    EXTERNAL_ACTION_DEDUPLICATED = "external_action.deduplicated"
    EXTERNAL_ACTION_RETRY = "external_action.retry"
    EXTERNAL_ACTION_RATE_LIMITED = "external_action.rate_limited"
    EXTERNAL_ACTION_BOUNDARY_DECISION = "external_action.boundary_decision"
    # Desktop product (Phase 10). Install, runtime lifecycle and browser
    # provisioning metadata only — never a secret, a token or a payload.
    DESKTOP_INSTALLED = "desktop.installed"
    DESKTOP_UNINSTALLED = "desktop.uninstalled"
    DESKTOP_RUNTIME_STARTED = "desktop.runtime.started"
    DESKTOP_RUNTIME_STOPPED = "desktop.runtime.stopped"
    BROWSER_PROVISION_REQUESTED = "desktop.browser.provision_requested"
    BROWSER_PROVISION_COMPLETED = "desktop.browser.provision_completed"
    BROWSER_PROVISION_FAILED = "desktop.browser.provision_failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AuditEvent(BaseModel):
    """A single immutable audit record."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id("evt"))
    type: AuditEventType
    tenant_id: str
    user_id: str
    run_id: str
    timestamp: datetime = Field(default_factory=_utcnow)
    #: Monotonic sequence within a run, for ordering guarantees.
    sequence: int = 0
    message: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def create(
        cls,
        event_type: AuditEventType,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        sequence: int = 0,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        return cls(
            type=event_type,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            sequence=sequence,
            message=message,
            data=redact(data or {}),
        )


class AuditSink(ABC):
    """Destination for audit events (memory, file, Postgres, SIEM)."""

    @abstractmethod
    async def emit(self, event: AuditEvent) -> None: ...

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        """Return events for a run if the sink supports reads."""
        return []

    async def next_sequence(self, *, run_id: str) -> int:
        """Return the next sequence number for a run, or 0 if unmanaged.

        Sinks that share durable storage across processes (e.g. Postgres) must
        override this and derive the sequence from that shared storage. An
        in-process counter is *not* sufficient: the API and a worker both write
        audit events for the same run, and an independent counter per process
        would collide on the ``(run_id, sequence)`` unique key. A sink that
        cannot know the sequence returns 0, leaving the caller's fallback in
        place.
        """
        return 0

    async def aclose(self) -> None:
        return None


class AuditLog:
    """Fan-out facade over one or more sinks.

    Recording must never break the run, so sink failures are swallowed (and
    logged) rather than propagated.

    Sequence numbers are assigned from shared storage when a sink can provide
    them (:meth:`AuditSink.next_sequence`), so the API and a worker writing the
    same run never collide on ``(run_id, sequence)``. The in-process counter is
    only a fallback for sinks that cannot answer (local/offline runtime).
    """

    def __init__(self, sinks: list[AuditSink] | None = None) -> None:
        self._sinks: list[AuditSink] = list(sinks or [])
        self._sequences: dict[str, int] = {}

    def add_sink(self, sink: AuditSink) -> None:
        self._sinks.append(sink)

    def next_sequence(self, run_id: str) -> int:
        current = self._sequences.get(run_id, 0) + 1
        self._sequences[run_id] = current
        return current

    async def _resolve_sequence(self, run_id: str) -> int:
        """Prefer a durable sequence from a sink, else the local counter."""
        for sink in self._sinks:
            sequence = await sink.next_sequence(run_id=run_id)
            if sequence:
                return sequence
        return self.next_sequence(run_id)

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        """Read a run's trail from the first sink that can answer.

        Sinks that cannot read return ``[]``; the first non-empty answer wins.
        A durable sink (Postgres/local) is registered first, so this returns the
        persisted trail rather than the process-local view.
        """
        for sink in self._sinks:
            events = await sink.list_for_run(tenant_id=tenant_id, run_id=run_id)
            if events:
                return events
        return []

    async def emit(self, event: AuditEvent) -> None:
        import logging

        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception:
                logging.getLogger(__name__).exception(
                    "audit sink %s failed to emit %s", type(sink).__name__, event.type.value
                )

    async def record(
        self,
        event_type: AuditEventType,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> AuditEvent:
        event = AuditEvent.create(
            event_type,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            sequence=await self._resolve_sequence(run_id),
            message=message,
            data=data,
        )
        await self.emit(event)
        return event

    async def history(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        events: list[AuditEvent] = []
        for sink in self._sinks:
            events.extend(await sink.list_for_run(tenant_id=tenant_id, run_id=run_id))
        return sorted(events, key=lambda e: (e.timestamp, e.sequence))

    async def aclose(self) -> None:
        for sink in self._sinks:
            await sink.aclose()


#: Re-exported so callers can construct the default run id when needed.
DEFAULT_RUN_PREFIX = PREFIX_RUN
