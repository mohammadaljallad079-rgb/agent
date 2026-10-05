"""Correlation context.

Every log line, metric and span carries the same correlation identifiers —
``request_id``, ``tenant_id``, ``user_id``, ``run_id``, ``job_id`` — so an
operator can follow one request from the API edge, through the queue, into a
worker and back. The context is a :class:`contextvars.ContextVar`, so it is
per-task/per-request and safe under concurrency (including asyncio tasks and
threads) without threading a parameter through every function.

Only identifiers are stored. No secret, token or payload ever enters the
context, which is what keeps it safe to emit verbatim.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace


@dataclass(frozen=True)
class CorrelationContext:
    """Immutable set of correlation identifiers for the current scope."""

    request_id: str | None = None
    tenant_id: str | None = None
    user_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    worker_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    step_run_id: str | None = None
    trigger_id: str | None = None
    scheduler_id: str | None = None

    def as_dict(self) -> dict[str, str]:
        """Return only the identifiers that are set, for log/trace fields."""
        return {
            key: value
            for key, value in {
                "request_id": self.request_id,
                "tenant_id": self.tenant_id,
                "user_id": self.user_id,
                "run_id": self.run_id,
                "job_id": self.job_id,
                "worker_id": self.worker_id,
                "workflow_id": self.workflow_id,
                "workflow_run_id": self.workflow_run_id,
                "step_run_id": self.step_run_id,
                "trigger_id": self.trigger_id,
                "scheduler_id": self.scheduler_id,
            }.items()
            if value is not None
        }


# A frozen dataclass of immutable str|None fields, so one shared instance is
# safe as a ContextVar default (no mutable state can leak between contexts).
_EMPTY_CONTEXT = CorrelationContext()
_CURRENT: ContextVar[CorrelationContext] = ContextVar(
    "arabic_agent_correlation", default=_EMPTY_CONTEXT
)


def current_context() -> CorrelationContext:
    """Return the correlation context bound to the current task."""
    return _CURRENT.get()


def current_run_id() -> str | None:
    return _CURRENT.get().run_id


def current_tenant_id() -> str | None:
    return _CURRENT.get().tenant_id


def bind_context(**fields: str | None) -> CorrelationContext:
    """Merge ``fields`` into the current context and return the new value.

    Callers that want scoping should prefer :func:`correlation_scope`, which
    restores the previous context on exit.
    """
    current = _CURRENT.get()
    updates = {k: v for k, v in fields.items() if v is not None}
    updated = replace(current, **updates)
    _CURRENT.set(updated)
    return updated


@contextmanager
def correlation_scope(**fields: str | None) -> Iterator[CorrelationContext]:
    """Run a block with ``fields`` merged into the correlation context.

    Restores the previous context on exit, even on exception, so a worker that
    processes many jobs never leaks one run's identifiers into the next.
    """
    updates = {k: v for k, v in fields.items() if v is not None}
    token = _CURRENT.set(replace(_CURRENT.get(), **updates))
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)
