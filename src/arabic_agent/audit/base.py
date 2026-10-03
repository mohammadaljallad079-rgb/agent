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
    MEMORY_WRITE = "memory.write"
    MEMORY_READ = "memory.read"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"


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

    async def aclose(self) -> None:
        return None


class AuditLog:
    """Fan-out facade over one or more sinks.

    Recording must never break the run, so sink failures are swallowed (and
    logged) rather than propagated.
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
            sequence=self.next_sequence(run_id),
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
