"""Observability unit tests: context, logging, metrics, tracing, readiness.

These exercise the real implementations — no doubles — so a regression in the
formatter, the Prometheus exposition or the readiness aggregation is caught.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from arabic_agent.observability.context import (
    CorrelationContext,
    bind_context,
    correlation_scope,
    current_context,
    current_run_id,
)
from arabic_agent.observability.logging import EXTRA_ATTR, JsonLogFormatter
from arabic_agent.observability.metrics import Metrics, MetricsRegistry, render_prometheus
from arabic_agent.observability.readiness import (
    ProbeResult,
    ReadinessProbe,
    ReadinessService,
)
from arabic_agent.observability.tracing import Tracer, span


# --- Correlation context ----------------------------------------------------
def test_context_binds_and_restores():
    assert current_context() == CorrelationContext()
    with correlation_scope(request_id="req_1", tenant_id="tnt_1"):
        assert current_run_id() is None
        with correlation_scope(run_id="run_1"):
            assert current_run_id() == "run_1"
            assert current_context().tenant_id == "tnt_1"
        # Inner scope restored: run id gone, request id still present.
        assert current_run_id() is None
        assert current_context().request_id == "req_1"
    assert current_context() == CorrelationContext()


def test_context_does_not_leak_across_tasks():
    async def worker(run_id: str) -> str | None:
        with correlation_scope(run_id=run_id):
            await asyncio.sleep(0)
            return current_run_id()

    async def main() -> tuple[str | None, str | None]:
        return await asyncio.gather(worker("run_a"), worker("run_b"))

    first, second = asyncio.run(main())
    assert first == "run_a"
    assert second == "run_b"


def test_bind_context_merges_without_scope():
    try:
        bind_context(tenant_id="tnt_x")
        assert current_context().tenant_id == "tnt_x"
        bind_context(user_id="usr_x")
        assert current_context().tenant_id == "tnt_x"
        assert current_context().user_id == "usr_x"
    finally:
        # Reset to a clean context for other tests.
        from arabic_agent.observability.context import _CURRENT

        _CURRENT.set(CorrelationContext())


# --- Structured logging -----------------------------------------------------
def _record(**fields: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )
    for key, value in fields.items():
        setattr(record, key, value)
    return record


def test_json_formatter_includes_correlation_and_extra():
    formatter = JsonLogFormatter(service="svc", environment="test")
    with correlation_scope(request_id="req_9", run_id="run_9"):
        payload = json.loads(formatter.format(_record()))
    assert payload["message"] == "hello"
    assert payload["service"] == "svc"
    assert payload["environment"] == "test"
    assert payload["request_id"] == "req_9"
    assert payload["run_id"] == "run_9"


def test_json_formatter_redacts_secrets():
    formatter = JsonLogFormatter()
    record = _record()
    setattr(record, EXTRA_ATTR, {"api_key": "sk-abcdefghijklmnop1234567890"})
    payload = json.loads(formatter.format(record))
    assert payload["api_key"] == "***REDACTED***"


def test_json_formatter_redacts_secret_in_message():
    formatter = JsonLogFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="token ghp_%s" % ("a" * 30),
        args=(),
        exc_info=None,
    )
    payload = json.loads(formatter.format(record))
    assert "ghp_" not in payload["message"]
    assert "***REDACTED***" in payload["message"]


# --- Metrics ----------------------------------------------------------------
def test_counter_gauge_histogram_values():
    metrics = Metrics()
    metrics.run_started(mode="inline")
    metrics.run_started(mode="inline")
    metrics.run_started(mode="queued")
    metrics.queue_depth(depth=7)
    metrics.run_finished(status="completed", duration_seconds=1.5)

    registry = metrics.registry
    assert registry.counter_value("aap_runs_started_total", labels={"mode": "inline"}) == 2
    assert registry.counter_value("aap_runs_started_total", labels={"mode": "queued"}) == 1
    assert registry.gauge_value("aap_queue_depth") == 7
    assert registry.histogram_count("aap_run_duration_seconds", labels={"status": "completed"}) == 1
    assert registry.histogram_sum("aap_run_duration_seconds", labels={"status": "completed"}) == 1.5


def test_prometheus_render_contains_help_type_and_labels():
    metrics = Metrics()
    metrics.job_enqueued(job_type="start_run")
    metrics.run_finished(status="completed", duration_seconds=0.2)
    text = render_prometheus(metrics.registry)
    assert "# HELP aap_jobs_enqueued_total" in text
    assert "# TYPE aap_jobs_enqueued_total counter" in text
    assert 'aap_jobs_enqueued_total{type="start_run"} 1' in text
    assert "# TYPE aap_run_duration_seconds histogram" in text
    assert "aap_run_duration_seconds_count" in text
    assert 'le="+Inf"' in text


def test_metric_labels_are_redacted():
    registry = MetricsRegistry()
    registry.inc("aap_test_total", labels={"key": "sk-abcdefghijklmnop1234567890"})
    text = registry.render()
    assert "sk-abcdefghijklmnop" not in text
    assert "***REDACTED***" in text


def test_registry_reset_clears_state():
    metrics = Metrics()
    metrics.run_started(mode="inline")
    metrics.registry.reset()
    assert metrics.registry.counter_value("aap_runs_started_total", labels={"mode": "inline"}) == 0


# --- Tracing ----------------------------------------------------------------
def test_tracer_records_nested_spans_with_trace_id():
    tracer = Tracer(enabled=True)
    with span("outer", tracer=tracer) as outer, span("inner", tracer=tracer) as inner:
        assert inner.trace_id == outer.trace_id
        assert inner.parent_id == outer.span_id
    recorded = tracer.recorded
    # Spans are recorded as they finish, so the inner span is appended first.
    assert [s.name for s in recorded] == ["inner", "outer"]
    assert all(s.end_time is not None for s in recorded)


def test_tracer_captures_error_status_and_reraises():
    tracer = Tracer(enabled=True)
    with pytest.raises(ValueError), span("boom", tracer=tracer):
        raise ValueError("nope")
    assert tracer.recorded[-1].status == "error"
    assert tracer.recorded[-1].error == "nope"


def test_tracer_exporter_receives_finished_spans():
    exported: list[str] = []
    tracer = Tracer(enabled=True, exporter=lambda s: exported.append(s.name))
    with span("exported", tracer=tracer):
        pass
    assert exported == ["exported"]


def test_disabled_tracer_does_not_record():
    tracer = Tracer(enabled=False)
    with span("noop", tracer=tracer):
        pass
    assert tracer.recorded == []


def test_span_attributes_carry_correlation_context():
    tracer = Tracer(enabled=True)
    with correlation_scope(run_id="run_ctx"), span("ctx", tracer=tracer):
        pass
    assert tracer.recorded[-1].attributes["run_id"] == "run_ctx"


# --- Readiness --------------------------------------------------------------
class _Probe(ReadinessProbe):
    def __init__(self, name: str, *, ok: bool, critical: bool = True, delay: float = 0.0) -> None:
        self.name = name
        self.critical = critical
        self._ok = ok
        self._delay = delay

    async def check(self) -> ProbeResult:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._result(self._ok, "detail", started=0.0)


async def test_readiness_all_ok():
    service = ReadinessService([_Probe("a", ok=True), _Probe("b", ok=True)])
    report = await service.check()
    assert report.ready is True
    assert report.failed_critical == ()


async def test_readiness_critical_failure_is_not_ready():
    service = ReadinessService([_Probe("a", ok=True), _Probe("b", ok=False)])
    report = await service.check()
    assert report.ready is False
    assert len(report.failed_critical) == 1


async def test_readiness_optional_failure_still_ready():
    service = ReadinessService([_Probe("a", ok=True), _Probe("opt", ok=False, critical=False)])
    report = await service.check()
    assert report.ready is True


async def test_readiness_timeout_is_failure():
    service = ReadinessService([_Probe("slow", ok=True, delay=0.5)], timeout_seconds=0.01)
    report = await service.check()
    assert report.ready is False
    assert report.results[0].detail == "timed out"


async def test_readiness_probe_exception_is_caught():
    class _Boom(ReadinessProbe):
        name = "boom"

        async def check(self) -> ProbeResult:
            raise RuntimeError("connection string leaked")

    service = ReadinessService([_Boom()])
    report = await service.check()
    assert report.ready is False
    # The internal error text is never surfaced over the probe result.
    assert report.results[0].detail == "unreachable"
    assert "leaked" not in report.results[0].detail
