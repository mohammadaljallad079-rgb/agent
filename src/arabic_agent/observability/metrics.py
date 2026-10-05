"""Metrics: a small, dependency-free registry with a Prometheus exposition.

The platform needs counters, gauges and histograms for the runtime paths that
matter operationally — runs, the queue, workers and recovery, tool calls and
retries, rate limiting, approvals, model calls and dependency health. Rather
than take on a client library, this module implements the primitives directly
and exposes them in Prometheus text format at ``/metrics``.

Cardinality and safety are enforced by construction: instrumentation never
passes a free-form value as a label. Label values are additionally passed
through the secret redactor, so a token accidentally used as a label cannot
leak into the exposition.
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from ..core.redaction import REDACTED, redact

#: Default histogram buckets (seconds), tuned for request/agent latency.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)


@dataclass
class _Histogram:
    buckets: tuple[float, ...]
    counts: list[int] = field(default_factory=list)
    total: float = 0.0
    observations: int = 0

    def __post_init__(self) -> None:
        if not self.counts:
            self.counts = [0] * len(self.buckets)

    def observe(self, value: float) -> None:
        self.total += value
        self.observations += 1
        for index, bound in enumerate(self.buckets):
            if value <= bound:
                self.counts[index] += 1


class MetricsRegistry:
    """Thread-safe in-process metrics store."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        self._histograms: dict[tuple[str, tuple[tuple[str, str], ...]], _Histogram] = {}
        self._help: dict[str, str] = {}

    # --- primitives --------------------------------------------------------
    def describe(self, name: str, help_text: str) -> None:
        self._help.setdefault(name, help_text)

    def inc(
        self, name: str, *, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        key = (name, _normalize(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0.0) + value

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None:
        key = (name, _normalize(labels))
        with self._lock:
            self._gauges[key] = value

    def observe(
        self,
        name: str,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> None:
        key = (name, _normalize(labels))
        with self._lock:
            histogram = self._histograms.get(key)
            if histogram is None:
                histogram = _Histogram(buckets=tuple(buckets))
                self._histograms[key] = histogram
            histogram.observe(value)

    def counter_value(self, name: str, *, labels: Mapping[str, str] | None = None) -> float:
        with self._lock:
            return self._counters.get((name, _normalize(labels)), 0.0)

    def gauge_value(self, name: str, *, labels: Mapping[str, str] | None = None) -> float | None:
        with self._lock:
            return self._gauges.get((name, _normalize(labels)))

    def histogram_count(self, name: str, *, labels: Mapping[str, str] | None = None) -> int:
        with self._lock:
            histogram = self._histograms.get((name, _normalize(labels)))
            return histogram.observations if histogram else 0

    def histogram_sum(self, name: str, *, labels: Mapping[str, str] | None = None) -> float:
        with self._lock:
            histogram = self._histograms.get((name, _normalize(labels)))
            return histogram.total if histogram else 0.0

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()

    # --- exposition --------------------------------------------------------
    def render(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        with self._lock:
            counters = dict(self._counters)
            gauges = dict(self._gauges)
            histograms = {k: _copy_histogram(v) for k, v in self._histograms.items()}
            helps = dict(self._help)

        lines: list[str] = []
        emitted: set[str] = set()

        for (name, labels), value in sorted(counters.items()):
            _emit_help_type(lines, helps, emitted, name, "counter")
            lines.append(f"{name}{_format_labels(labels)} {_format_number(value)}")

        for (name, labels), value in sorted(gauges.items()):
            _emit_help_type(lines, helps, emitted, name, "gauge")
            lines.append(f"{name}{_format_labels(labels)} {_format_number(value)}")

        for (name, labels), histogram in sorted(histograms.items()):
            _emit_help_type(lines, helps, emitted, name, "histogram")
            for bound, count in zip(histogram.buckets, histogram.counts, strict=True):
                bucket_labels = (*labels, ("le", _format_number(bound)))
                lines.append(
                    f"{name}_bucket{_format_labels(bucket_labels)} {_format_number(count)}"
                )
            inf_labels = (*labels, ("le", "+Inf"))
            inf_count = _format_number(histogram.observations)
            lines.append(f"{name}_bucket{_format_labels(inf_labels)} {inf_count}")
            lines.append(f"{name}_sum{_format_labels(labels)} {_format_number(histogram.total)}")
            lines.append(
                f"{name}_count{_format_labels(labels)} {_format_number(histogram.observations)}"
            )

        return "\n".join(lines) + ("\n" if lines else "")


def _normalize(labels: Mapping[str, str] | None) -> tuple[tuple[str, str], ...]:
    if not labels:
        return ()
    # Redact label values as defense in depth: a secret must never appear in
    # the exposition even if a caller misuses a label.
    safe = {str(k): _safe_label_value(v) for k, v in labels.items()}
    return tuple(sorted(safe.items()))


def _safe_label_value(value: object) -> str:
    text = str(value)
    if redact(text) != text:
        return REDACTED
    return text


def _copy_histogram(histogram: _Histogram) -> _Histogram:
    return _Histogram(
        buckets=histogram.buckets,
        counts=list(histogram.counts),
        total=histogram.total,
        observations=histogram.observations,
    )


def _emit_help_type(
    lines: list[str], helps: dict[str, str], emitted: set[str], name: str, kind: str
) -> None:
    if name in emitted:
        return
    emitted.add(name)
    lines.append(f"# HELP {name} {helps.get(name, name)}")
    lines.append(f"# TYPE {name} {kind}")


def _format_labels(labels: tuple[tuple[str, str], ...]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
    return "{" + inner + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_number(value: float) -> str:
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value):
        return str(int(value))
    return repr(value)


class Metrics:
    """Typed facade over a registry, with the platform's metric names.

    Instrumentation calls a named method rather than a raw metric string, which
    keeps label names and cardinality consistent and makes the set of emitted
    metrics auditable in one place.
    """

    def __init__(self, registry: MetricsRegistry | None = None) -> None:
        self.registry = registry or MetricsRegistry()
        self._describe_all()

    def _describe_all(self) -> None:
        for name, text in _HELP.items():
            self.registry.describe(name, text)

    # --- runs --------------------------------------------------------------
    def run_started(self, *, mode: str) -> None:
        self.registry.inc("aap_runs_started_total", labels={"mode": mode})

    def run_finished(self, *, status: str, duration_seconds: float) -> None:
        self.registry.inc("aap_runs_finished_total", labels={"status": status})
        self.registry.observe(
            "aap_run_duration_seconds", duration_seconds, labels={"status": status}
        )

    def run_recovered(self) -> None:
        self.registry.inc("aap_runs_recovered_total")

    def run_failed(self, *, reason: str) -> None:
        self.registry.inc("aap_runs_failed_total", labels={"reason": reason})

    # --- workflows ---------------------------------------------------------
    def workflow_run_started(self) -> None:
        self.registry.inc("aap_workflow_runs_started_total")

    def workflow_run_finished(self, *, status: str, duration_seconds: float) -> None:
        self.registry.inc("aap_workflow_runs_finished_total", labels={"status": status})
        self.registry.observe(
            "aap_workflow_run_duration_seconds", duration_seconds, labels={"status": status}
        )

    def workflow_step(self, *, kind: str, outcome: str, duration_seconds: float) -> None:
        self.registry.inc("aap_workflow_steps_total", labels={"kind": kind, "outcome": outcome})
        self.registry.observe(
            "aap_workflow_step_duration_seconds", duration_seconds, labels={"kind": kind}
        )

    def workflow_step_retry(self, *, kind: str) -> None:
        self.registry.inc("aap_workflow_step_retries_total", labels={"kind": kind})

    def workflow_run_recovered(self) -> None:
        self.registry.inc("aap_workflow_runs_recovered_total")

    # --- triggers ----------------------------------------------------------
    def trigger_fired(self, *, kind: str, outcome: str) -> None:
        self.registry.inc("aap_trigger_fires_total", labels={"kind": kind, "outcome": outcome})

    def trigger_skipped(self, *, kind: str, reason: str) -> None:
        self.registry.inc("aap_trigger_skips_total", labels={"kind": kind, "reason": reason})

    def webhook_rejected(self, *, reason: str) -> None:
        self.registry.inc("aap_webhook_rejected_total", labels={"reason": reason})

    def recovery_attempt(self, *, outcome: str) -> None:
        self.registry.inc("aap_recovery_attempts_total", labels={"outcome": outcome})

    # --- queue -------------------------------------------------------------
    def job_enqueued(self, *, job_type: str) -> None:
        self.registry.inc("aap_jobs_enqueued_total", labels={"type": job_type})

    def job_claimed(self, *, job_type: str) -> None:
        self.registry.inc("aap_jobs_claimed_total", labels={"type": job_type})

    def job_acked(self, *, job_type: str) -> None:
        self.registry.inc("aap_jobs_acked_total", labels={"type": job_type})

    def job_nacked(self, *, job_type: str, requeue: bool) -> None:
        self.registry.inc(
            "aap_jobs_nacked_total",
            labels={"type": job_type, "requeue": "true" if requeue else "false"},
        )

    def job_failed(self, *, job_type: str) -> None:
        self.registry.inc("aap_jobs_failed_total", labels={"type": job_type})

    def jobs_reclaimed(self, *, count: int) -> None:
        self.registry.inc("aap_jobs_reclaimed_total", value=float(count))

    def queue_depth(self, *, depth: int) -> None:
        self.registry.set_gauge("aap_queue_depth", float(depth))

    # --- tools -------------------------------------------------------------
    def tool_call(self, *, tool: str, outcome: str, duration_seconds: float) -> None:
        self.registry.inc("aap_tool_calls_total", labels={"tool": tool, "outcome": outcome})
        self.registry.observe("aap_tool_duration_seconds", duration_seconds, labels={"tool": tool})

    def tool_retry(self, *, tool: str) -> None:
        self.registry.inc("aap_tool_retries_total", labels={"tool": tool})

    # --- rate limiting -----------------------------------------------------
    def rate_limit_denied(self, *, scope: str) -> None:
        self.registry.inc("aap_rate_limit_denied_total", labels={"scope": scope})

    # --- approvals ---------------------------------------------------------
    def approval_requested(self) -> None:
        self.registry.inc("aap_approvals_requested_total")

    def approval_resolved(self, *, decision: str, wait_seconds: float | None = None) -> None:
        self.registry.inc("aap_approvals_resolved_total", labels={"decision": decision})
        if wait_seconds is not None:
            self.registry.observe("aap_approval_wait_seconds", wait_seconds)

    # --- models ------------------------------------------------------------
    def model_call(self, *, model: str, outcome: str, duration_seconds: float) -> None:
        self.registry.inc("aap_model_calls_total", labels={"model": model, "outcome": outcome})
        self.registry.observe(
            "aap_model_duration_seconds", duration_seconds, labels={"model": model}
        )

    def model_retry(self, *, model: str) -> None:
        self.registry.inc("aap_model_retries_total", labels={"model": model})

    # --- dependency health -------------------------------------------------
    def dependency_up(self, *, dependency: str, up: bool) -> None:
        self.registry.set_gauge(
            "aap_dependency_up", 1.0 if up else 0.0, labels={"dependency": dependency}
        )


_HELP: dict[str, str] = {
    "aap_runs_started_total": "Total runs accepted, by execution mode.",
    "aap_runs_finished_total": "Total runs reaching a terminal state, by status.",
    "aap_run_duration_seconds": "Run wall-clock duration in seconds, by final status.",
    "aap_runs_recovered_total": "Runs recovered after worker loss.",
    "aap_runs_failed_total": "Runs failed by the worker, by reason.",
    "aap_workflow_runs_started_total": "Workflow runs accepted.",
    "aap_workflow_runs_finished_total": "Workflow runs reaching a terminal state, by status.",
    "aap_workflow_run_duration_seconds": "Workflow run wall-clock duration, by final status.",
    "aap_workflow_steps_total": "Workflow step executions, by kind and outcome.",
    "aap_workflow_step_duration_seconds": "Workflow step duration in seconds, by kind.",
    "aap_workflow_step_retries_total": "Workflow step retries, by kind.",
    "aap_workflow_runs_recovered_total": "Workflow runs recovered after worker loss.",
    "aap_trigger_fires_total": "Trigger firings, by kind and outcome.",
    "aap_trigger_skips_total": "Trigger firings skipped, by kind and reason.",
    "aap_webhook_rejected_total": "Webhook deliveries rejected, by reason.",
    "aap_recovery_attempts_total": "Recovery sweep outcomes, by result.",
    "aap_jobs_enqueued_total": "Jobs enqueued, by job type.",
    "aap_jobs_claimed_total": "Jobs claimed by a worker, by job type.",
    "aap_jobs_acked_total": "Jobs acknowledged successfully, by job type.",
    "aap_jobs_nacked_total": "Jobs returned to the queue, by type and requeue flag.",
    "aap_jobs_failed_total": "Jobs that exhausted their delivery attempts, by type.",
    "aap_jobs_reclaimed_total": "Jobs reclaimed from dead workers.",
    "aap_queue_depth": "Approximate number of ready jobs.",
    "aap_tool_calls_total": "Tool executions, by tool and outcome.",
    "aap_tool_duration_seconds": "Tool execution duration in seconds, by tool.",
    "aap_tool_retries_total": "Tool retries, by tool.",
    "aap_rate_limit_denied_total": "Requests rejected by the rate limiter, by scope.",
    "aap_approvals_requested_total": "Approvals requested.",
    "aap_approvals_resolved_total": "Approvals resolved, by decision.",
    "aap_approval_wait_seconds": "Time from approval request to resolution, in seconds.",
    "aap_model_calls_total": "Model calls, by model and outcome.",
    "aap_model_duration_seconds": "Model call duration in seconds, by model.",
    "aap_model_retries_total": "Model call retries, by model.",
    "aap_dependency_up": "Dependency reachability (1 up, 0 down), by dependency.",
}


#: Process-wide default registry and facade. Tests construct their own
#: :class:`Metrics` to avoid cross-test coupling.
_DEFAULT = MetricsRegistry()


def render_prometheus(registry: MetricsRegistry | None = None) -> str:
    """Render the given registry (or the process default) as Prometheus text."""
    return (registry or _DEFAULT).render()


def default_metrics() -> Metrics:
    """Return a :class:`Metrics` facade over the process default registry."""
    return Metrics(_DEFAULT)
