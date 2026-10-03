"""Optional OpenTelemetry bridge.

OpenTelemetry is *not* a hard dependency: the platform must run offline and in
tests with no collector. When the SDK is installed and ``AAP_OTEL_ENABLED`` is
set, this module returns an exporter that forwards each finished
:class:`~arabic_agent.observability.tracing.Span` to an OpenTelemetry tracer, so
spans land in whatever backend the operator configured (OTLP, Jaeger, ...).

If the SDK is absent, :func:`otel_exporter` returns ``None`` and the in-process
tracer remains the whole story — honest, rather than pretending a collector is
wired.
"""

from __future__ import annotations

import logging

from .tracing import Exporter, Span

logger = logging.getLogger(__name__)


def otel_available() -> bool:
    """Whether the OpenTelemetry API is importable in this environment."""
    try:
        import opentelemetry.trace  # noqa: F401
    except Exception:
        return False
    return True


def otel_exporter(service_name: str = "arabic-agent") -> Exporter | None:
    """Return an exporter forwarding spans to OpenTelemetry, or ``None``.

    The exporter is best-effort: it never raises into the agent, and it only
    carries the identifiers and small attributes already on the span.
    """
    try:
        from opentelemetry import trace as otel_trace
        from opentelemetry.trace import Status, StatusCode
    except Exception:
        logger.info("OpenTelemetry not installed; using in-process tracing only")
        return None

    tracer = otel_trace.get_tracer(service_name)

    def export(span: Span) -> None:
        otel_span = tracer.start_span(
            span.name,
            start_time=_to_ns(span.start_time),
            attributes={k: str(v) for k, v in span.attributes.items()},
        )
        for event_name, event_attrs in span.events:
            otel_span.add_event(event_name, attributes={k: str(v) for k, v in event_attrs.items()})
        if span.status == "error":
            otel_span.set_status(Status(StatusCode.ERROR, span.error or "error"))
        otel_span.end(end_time=_to_ns(span.end_time) if span.end_time else None)

    return export


def _to_ns(moment: object) -> int:
    from datetime import datetime

    if isinstance(moment, datetime):
        return int(moment.timestamp() * 1_000_000_000)
    return 0
