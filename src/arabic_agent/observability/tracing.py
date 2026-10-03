"""Tracing.

A minimal tracing facade that is useful without a collector and integrates with
OpenTelemetry when it is installed. Spans are created in-process, tagged with
the current correlation identifiers, and handed to a configurable exporter.

Why not depend on the OpenTelemetry SDK directly: the platform must run offline
and in tests with no collector, and the agent core must not import a vendor SDK.
Instead, :class:`Tracer` records spans in memory (so tests can assert on them)
and optionally forwards each finished span to an exporter. If
``opentelemetry`` is available, :func:`configure_tracing` can install the
OpenTelemetry exporter bridge; otherwise the in-process tracer is the whole
story, which is honest rather than pretending a collector is wired.

Spans only ever carry identifiers and small scalars. Attribute values pass
through the secret redactor before they are stored.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ..core.redaction import REDACTED, redact
from .context import current_context

logger = logging.getLogger(__name__)


@dataclass
class Span:
    """A single unit of traced work."""

    name: str
    trace_id: str
    span_id: str
    parent_id: str | None
    start_time: datetime
    attributes: dict[str, Any] = field(default_factory=dict)
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    status: str = "unset"
    error: str | None = None
    end_time: datetime | None = None

    @property
    def duration_seconds(self) -> float:
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time).total_seconds()

    def set_attribute(self, key: str, value: Any) -> None:
        self.attributes[key] = _safe(value)

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.events.append((name, {k: _safe(v) for k, v in (attributes or {}).items()}))

    def finish(self, *, status: str = "ok", error: str | None = None) -> None:
        self.status = status
        self.error = redact(error) if error else None
        self.end_time = datetime.now(UTC)


Exporter = Callable[[Span], None]

_CURRENT_SPAN: ContextVar[Span | None] = ContextVar("arabic_agent_span", default=None)


class Tracer:
    """Creates and records spans."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        service: str = "arabic-agent",
        exporter: Exporter | None = None,
        max_recorded: int = 1000,
    ) -> None:
        self.enabled = enabled
        self.service = service
        self._exporter = exporter
        self._max_recorded = max_recorded
        self._recorded: list[Span] = []

    @property
    def recorded(self) -> list[Span]:
        """Finished spans kept in memory (for tests and local debugging)."""
        return list(self._recorded)

    def start_span(self, name: str, *, attributes: dict[str, Any] | None = None) -> Span:
        parent = _CURRENT_SPAN.get()
        trace_id = parent.trace_id if parent is not None else uuid.uuid4().hex
        span = Span(
            name=name,
            trace_id=trace_id,
            span_id=uuid.uuid4().hex[:16],
            parent_id=parent.span_id if parent is not None else None,
            start_time=datetime.now(UTC),
        )
        span.attributes.update(current_context().as_dict())
        for key, value in (attributes or {}).items():
            span.set_attribute(key, value)
        return span

    def finish_span(self, span: Span, *, status: str = "ok", error: str | None = None) -> None:
        span.finish(status=status, error=error)
        if not self.enabled:
            return
        self._recorded.append(span)
        if len(self._recorded) > self._max_recorded:
            del self._recorded[: len(self._recorded) - self._max_recorded]
        if self._exporter is not None:
            try:
                self._exporter(span)
            except Exception:
                logger.warning("trace exporter failed for span %s", span.name, exc_info=True)


_DEFAULT_TRACER = Tracer(enabled=False)


def get_tracer() -> Tracer:
    """Return the process-wide tracer."""
    return _DEFAULT_TRACER


def configure_tracing(
    *,
    enabled: bool,
    service: str = "arabic-agent",
    exporter: Exporter | None = None,
) -> Tracer:
    """Configure and return the process-wide tracer."""
    global _DEFAULT_TRACER
    _DEFAULT_TRACER = Tracer(enabled=enabled, service=service, exporter=exporter)
    return _DEFAULT_TRACER


def current_span() -> Span | None:
    return _CURRENT_SPAN.get()


@contextmanager
def span(
    name: str,
    *,
    attributes: dict[str, Any] | None = None,
    tracer: Tracer | None = None,
) -> Iterator[Span]:
    """Run a block inside a span, recording it on exit (even on exception)."""
    active = tracer or _DEFAULT_TRACER
    if not active.enabled:
        # Still yield a span so callers can attach attributes unconditionally,
        # but do not record or export it.
        yield active.start_span(name, attributes=attributes)
        return

    child = active.start_span(name, attributes=attributes)
    token = _CURRENT_SPAN.set(child)
    try:
        yield child
    except Exception as exc:
        active.finish_span(child, status="error", error=str(exc))
        raise
    else:
        active.finish_span(child, status="ok")
    finally:
        _CURRENT_SPAN.reset(token)


def _safe(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    # Do not store arbitrary objects in spans; a str() that happens to contain a
    # secret is still caught by the redactor.
    return redact(str(value)) if redact(str(value)) != str(value) else REDACTED
