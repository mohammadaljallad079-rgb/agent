"""Internal domain event bus.

A minimal publish/subscribe channel for *internal* events — the platform's own
services emitting facts like ``invoice.created`` so an EVENT trigger can react
to them. This is deliberately not a message broker: it is in-process, delivers
to subscribers asynchronously, and never blocks the publisher (a slow or failing
subscriber is isolated and logged, never propagated).

Cross-process delivery is out of scope here on purpose. A scheduled or
webhook-driven run is already durable; an in-process event bus keeps the first
slice of the feature honest rather than pretending to be Kafka.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.redaction import redact

logger = logging.getLogger(__name__)

#: A subscriber receives the event name and its (redacted) payload.
EventHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class DomainEvent:
    """A fact that happened inside the platform."""

    type: str
    tenant_id: str
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class EventBus:
    """In-process publish/subscribe for domain events."""

    def __init__(self) -> None:
        self._subscribers: list[EventHandler] = []

    def subscribe(self, handler: EventHandler) -> None:
        """Register a handler for every published event."""
        self._subscribers.append(handler)

    def unsubscribe(self, handler: EventHandler) -> None:
        self._subscribers = [h for h in self._subscribers if h is not handler]

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: DomainEvent) -> None:
        """Deliver ``event`` to every subscriber, isolating failures.

        Subscribers run concurrently; one raising must not stop the others or
        the publisher. Payloads are redacted before they leave the bus so a
        secret cannot be fanned out to subscribers.
        """
        if not self._subscribers:
            return
        safe_payload = redact(dict(event.payload))
        results = await asyncio.gather(
            *(self._deliver(handler, event.type, safe_payload) for handler in self._subscribers),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("event subscriber failed for %s", event.type, exc_info=result)

    async def _deliver(
        self, handler: EventHandler, event_type: str, payload: dict[str, Any]
    ) -> None:
        await handler(event_type, payload)
