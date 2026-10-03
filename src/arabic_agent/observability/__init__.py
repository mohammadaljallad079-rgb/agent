"""Observability: correlation context, structured logging, metrics and traces.

This package is deliberately dependency-light. Metrics and traces are implemented
in-process (with an optional OpenTelemetry bridge) so the platform has real,
testable observability without mandating a collector or an extra runtime
dependency. The agent core depends on these interfaces, never on a vendor SDK.
"""

from __future__ import annotations

from .context import (
    CorrelationContext,
    bind_context,
    correlation_scope,
    current_context,
    current_run_id,
    current_tenant_id,
)
from .logging import JsonLogFormatter, configure_logging
from .metrics import Metrics, MetricsRegistry, render_prometheus
from .tracing import (
    Span,
    Tracer,
    configure_tracing,
    current_span,
    get_tracer,
    span,
)


def configure_observability(settings: object) -> None:
    """Configure process-wide logging and tracing from a Settings object.

    Kept in this dependency-light package so both the API and the worker can
    call it without importing each other (or FastAPI).
    """
    import logging as _logging

    from .tracing import Exporter

    log_json = bool(getattr(settings, "log_json", True))
    if log_json:
        level_name = str(getattr(settings, "log_level", "INFO")).upper()
        configure_logging(
            level=getattr(_logging, level_name, _logging.INFO),
            service=str(getattr(settings, "otel_service_name", "arabic-agent")),
            environment=str(getattr(settings, "environment", "development")),
        )
    exporter: Exporter | None = None
    if bool(getattr(settings, "otel_enabled", False)):
        from .otel import otel_exporter

        exporter = otel_exporter(str(getattr(settings, "otel_service_name", "arabic-agent")))
    configure_tracing(
        enabled=True,
        service=str(getattr(settings, "otel_service_name", "arabic-agent")),
        exporter=exporter,
    )


__all__ = [
    "CorrelationContext",
    "JsonLogFormatter",
    "Metrics",
    "MetricsRegistry",
    "Span",
    "Tracer",
    "bind_context",
    "configure_logging",
    "configure_observability",
    "configure_tracing",
    "correlation_scope",
    "current_context",
    "current_run_id",
    "current_span",
    "current_tenant_id",
    "get_tracer",
    "render_prometheus",
    "span",
]
