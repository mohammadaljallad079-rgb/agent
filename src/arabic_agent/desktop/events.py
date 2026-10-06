"""Desktop event broker: real-time run events for the UI.

The desktop UI shows the agent *working* — planning, calling a tool, waiting for
an approval, verifying, finishing. That progress must come from real runtime
state, never from a timer. This broker is the seam:

* :class:`AuditSink` is attached to the platform's audit log, so **every** event
  the agent already emits (plan, step, tool call/result, approval, model call,
  run lifecycle) is mirrored to subscribers with no change to the agent core.
* Audit payloads are already redacted at construction, and this broker applies
  :func:`redact` again as defence in depth, so a secret cannot reach the UI.
* Subscribers receive a bounded, per-tenant stream; a slow or failed subscriber
  is isolated (never blocks the agent) and dropped rather than buffering
  unboundedly.

Nothing here fabricates progress. An event exists only because the runtime
recorded it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..audit.base import AuditEvent, AuditSink
from ..core.redaction import redact

logger = logging.getLogger(__name__)

#: Maximum events buffered for one subscriber before the oldest are dropped.
_MAX_QUEUE = 500


class DesktopEvent(BaseModel):
    """A UI-facing view of one runtime event (safe to serialise and display)."""

    model_config = ConfigDict(frozen=True)

    type: str
    run_id: str
    tenant_id: str
    message: str = ""
    data: dict[str, Any] = {}
    sequence: int = 0
    timestamp: datetime

    @classmethod
    def from_audit(cls, event: AuditEvent) -> DesktopEvent:
        return cls(
            type=event.type.value,
            run_id=event.run_id,
            tenant_id=event.tenant_id,
            message=event.message,
            data=redact(event.data),
            sequence=event.sequence,
            timestamp=event.timestamp,
        )


class _Subscriber:
    """One bounded event queue, keyed by tenant."""

    __slots__ = ("queue", "tenant_id")

    def __init__(self, *, tenant_id: str | None) -> None:
        self.tenant_id = tenant_id
        self.queue: asyncio.Queue[DesktopEvent] = asyncio.Queue(maxsize=_MAX_QUEUE)

    def offer(self, event: DesktopEvent) -> None:
        if self.tenant_id is not None and event.tenant_id != self.tenant_id:
            return
        if self.queue.full():
            # Drop the oldest rather than block the agent or grow unboundedly.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            self.queue.put_nowait(event)


class DesktopEventBroker(AuditSink):
    """An :class:`AuditSink` that fans events out to live UI subscribers."""

    def __init__(self) -> None:
        self._subscribers: list[_Subscriber] = []
        self._recent: list[DesktopEvent] = []
        self._recent_limit = 200

    # -- AuditSink -------------------------------------------------------
    async def emit(self, event: AuditEvent) -> None:
        desktop_event = DesktopEvent.from_audit(event)
        self._recent.append(desktop_event)
        if len(self._recent) > self._recent_limit:
            del self._recent[: len(self._recent) - self._recent_limit]
        for subscriber in list(self._subscribers):
            subscriber.offer(desktop_event)

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[AuditEvent]:
        # This sink does not persist events; it is a live fan-out only.
        return []

    # -- Subscription ----------------------------------------------------
    def subscribe(self, *, tenant_id: str | None = None) -> _Subscriber:
        subscriber = _Subscriber(tenant_id=tenant_id)
        self._subscribers.append(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: _Subscriber) -> None:
        self._subscribers = [s for s in self._subscribers if s is not subscriber]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def recent(self, *, run_id: str | None = None, limit: int = 100) -> list[DesktopEvent]:
        """Return recently observed events (newest last), for replay on connect."""
        events = self._recent
        if run_id is not None:
            events = [e for e in events if e.run_id == run_id]
        return events[-limit:]

    async def stream(
        self, *, tenant_id: str | None = None, replay: bool = True
    ) -> AsyncIterator[DesktopEvent]:
        """Yield events for a subscriber until cancelled.

        With ``replay`` the recent buffer is replayed first, so a UI that
        connects just after a run started still sees its beginning.
        """
        subscriber = self.subscribe(tenant_id=tenant_id)
        try:
            if replay:
                for event in self.recent(limit=50):
                    if tenant_id is None or event.tenant_id == tenant_id:
                        yield event
            while True:
                yield await subscriber.queue.get()
        finally:
            self.unsubscribe(subscriber)


def sse_format(event: DesktopEvent, *, event_name: str = "agent") -> str:
    """Render a :class:`DesktopEvent` as a Server-Sent Events frame."""
    import json

    payload = event.model_dump(mode="json")
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "DesktopEvent",
    "DesktopEventBroker",
    "sse_format",
]
