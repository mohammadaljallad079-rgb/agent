"""Bounded retries and backoff for external calls.

External systems fail transiently: a 429, a temporary 5xx, a network timeout.
Retrying is correct, but *unbounded* retrying is not — it hammers the provider
and can turn one duplicate action into many. This module provides one small,
bounded policy used by every connector:

* a bounded attempt count;
* exponential backoff with jitter (so a fleet of clients does not synchronise);
* honouring a provider's ``Retry-After`` when present (429 / 503);
* an explicit classification of which errors are retryable, so a permanent
  error (400/404/422) is never retried blindly.

The policy never decides *whether* an action is safe to retry — that is the
ledger's job (a consequential action that ended ``UNKNOWN`` must be verified, not
retried). This policy only bounds the transport-level retry of a *safe* read or
of an action already cleared for retry.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from ..core.errors import PlatformError, TransientError

T = TypeVar("T")

#: HTTP statuses that are transiently retryable.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class RetryPolicy:
    """A bounded retry/backoff policy."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    #: Multiplier applied per attempt (exponential backoff).
    factor: float = 2.0
    #: Fraction of jitter (0..1) applied to each delay.
    jitter: float = 0.25

    def delay_for(self, attempt: int, *, retry_after: float | None = None) -> float:
        """Seconds to wait before ``attempt`` (1-based). Honours ``Retry-After``."""
        if retry_after is not None and retry_after >= 0:
            return min(retry_after, self.max_delay_seconds)
        raw = self.base_delay_seconds * (self.factor ** max(0, attempt - 1))
        capped = min(raw, self.max_delay_seconds)
        if self.jitter > 0:
            spread = capped * self.jitter
            # Jitter spreads a backoff, it is not a security decision; the
            # non-cryptographic PRNG is the right tool here.
            capped = max(0.0, capped - spread + random.random() * 2 * spread)  # noqa: S311
        return capped


def is_retryable_status(status: int) -> bool:
    return status in RETRYABLE_STATUS


async def with_retry(
    operation: Callable[[int], Awaitable[T]],
    *,
    policy: RetryPolicy,
    retry_after: Callable[[Exception], float | None] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
) -> T:
    """Run ``operation(attempt)`` with bounded retries on transient failures.

    ``operation`` receives the 1-based attempt number and may raise. A
    :class:`TransientError` (or a classified retryable :class:`PlatformError`)
    is retried up to ``policy.max_attempts``; anything else propagates at once.
    The final failure is re-raised unchanged so the caller can classify it.
    """
    waiter = sleep or asyncio.sleep
    last: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation(attempt)
        except PlatformError as exc:
            if not exc.retryable:
                raise
            last = exc
            if attempt >= policy.max_attempts:
                break
            wait = policy.delay_for(attempt, retry_after=retry_after(exc) if retry_after else None)
            await waiter(wait)
    assert last is not None  # pragma: no cover - loop guarantees a failure
    raise last


def transient(message: str, *, retry_after: float | None = None) -> TransientError:
    """Build a transient error, optionally carrying a ``Retry-After`` hint."""
    error = TransientError(message)
    if retry_after is not None:
        error.details["retry_after"] = retry_after
    return error


__all__ = ["RETRYABLE_STATUS", "RetryPolicy", "is_retryable_status", "transient", "with_retry"]
