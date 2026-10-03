"""In-process rate limiting.

A token-bucket limiter keyed by principal (falling back to client IP). This is
the local/single-node implementation; a multi-node deployment swaps in a Redis
backend behind the same interface. It is real enforcement, not a no-op stub.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class RateLimiter:
    """Token-bucket rate limiter."""

    rate_per_minute: int
    burst: int = 0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.burst <= 0:
            self.burst = max(1, self.rate_per_minute)
        self._refill_rate = self.rate_per_minute / 60.0

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Consume one token for ``key``; return whether the call is allowed."""
        now = now if now is not None else time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(tokens=float(self.burst) - 1.0, last_refill=now)
            return True
        elapsed = now - bucket.last_refill
        bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self._refill_rate)
        bucket.last_refill = now
        if bucket.tokens >= 1.0:
            bucket.tokens -= 1.0
            return True
        return False

    def retry_after_seconds(self, key: str) -> float:
        bucket = self._buckets.get(key)
        if bucket is None or bucket.tokens >= 1.0:
            return 0.0
        needed = 1.0 - bucket.tokens
        return round(needed / self._refill_rate, 2) if self._refill_rate else 60.0
