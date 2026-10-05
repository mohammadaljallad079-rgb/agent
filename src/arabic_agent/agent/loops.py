"""Loop protection.

The agent loop is already bounded by max steps, max retries, max replans and a
wall-clock deadline. This guard adds a finer, *semantic* bound: it watches the
sequence of tool calls so a model that repeats the same action (or the same
failure) is stopped with a clear, auditable reason instead of spinning until a
coarse limit trips.

It is deliberately small and deterministic — a counter keyed by a call
signature — so its behaviour is easy to reason about and to test. It records
only signatures and counts; tool arguments are hashed, never stored verbatim.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

#: How many times the exact same (tool, arguments) call is tolerated before the
#: guard tells the model to change approach.
DEFAULT_REPEAT_SOFT_LIMIT = 2
#: How many times it is tolerated before the run is terminated.
DEFAULT_REPEAT_HARD_LIMIT = 4
#: How many identical failures of the same (tool, error class) are tolerated.
DEFAULT_FAILURE_LIMIT = 3


def call_signature(tool_name: str, arguments: dict[str, Any]) -> str:
    """A stable, non-reversible signature for a tool call."""
    payload = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{tool_name}:{digest}"


@dataclass(frozen=True)
class LoopVerdict:
    """The guard's opinion about the current call."""

    action: str  # "allow" | "warn" | "abort"
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.action == "allow"


@dataclass
class LoopGuard:
    """Tracks repeated calls and repeated failures within a single run."""

    repeat_soft_limit: int = DEFAULT_REPEAT_SOFT_LIMIT
    repeat_hard_limit: int = DEFAULT_REPEAT_HARD_LIMIT
    failure_limit: int = DEFAULT_FAILURE_LIMIT
    _call_counts: dict[str, int] = field(default_factory=dict)
    _failure_counts: dict[str, int] = field(default_factory=dict)

    def observe_call(self, tool_name: str, arguments: dict[str, Any]) -> LoopVerdict:
        """Record an *about-to-run* call and return a verdict.

        A read-only tool that is repeated is wasteful but harmless; the guard
        still counts it, because a repeated identical read usually means the
        model is stuck rather than making progress.
        """
        signature = call_signature(tool_name, arguments)
        count = self._call_counts.get(signature, 0) + 1
        self._call_counts[signature] = count
        if count >= self.repeat_hard_limit:
            return LoopVerdict(
                action="abort",
                reason=(
                    f"tool {tool_name!r} was called with identical arguments "
                    f"{count} times; aborting to avoid a loop"
                ),
            )
        if count > self.repeat_soft_limit:
            return LoopVerdict(
                action="warn",
                reason=(
                    f"tool {tool_name!r} was already called with identical "
                    f"arguments {count - 1} time(s); change your approach"
                ),
            )
        return LoopVerdict(action="allow")

    def observe_failure(self, tool_name: str, error_class: str) -> LoopVerdict:
        """Record a failure and return a verdict on repeating it."""
        key = f"{tool_name}:{error_class}"
        count = self._failure_counts.get(key, 0) + 1
        self._failure_counts[key] = count
        if count >= self.failure_limit:
            return LoopVerdict(
                action="abort",
                reason=(
                    f"{tool_name!r} failed with the same error class "
                    f"({error_class}) {count} times; aborting"
                ),
            )
        return LoopVerdict(action="allow")


__all__ = [
    "DEFAULT_FAILURE_LIMIT",
    "DEFAULT_REPEAT_HARD_LIMIT",
    "DEFAULT_REPEAT_SOFT_LIMIT",
    "LoopGuard",
    "LoopVerdict",
    "call_signature",
]
