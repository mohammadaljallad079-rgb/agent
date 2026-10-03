"""Ephemeral infrastructure: Redis client, rate limiting, locks, idempotency.

Everything here is coordination, not truth. The durable source of truth is
Postgres; a local/offline runtime can operate with none of this.
"""

from __future__ import annotations

from .idempotency import (
    IdempotencyGuard,
    IdempotencyOutcome,
    IdempotencyStatus,
    InMemoryIdempotencyGuard,
    RedisIdempotencyGuard,
    digest,
    make_key,
)
from .lock import RedisLock, redis_lock
from .rate_limit import (
    InMemoryRateLimiter,
    RateLimiterBackend,
    RedisRateLimiter,
)
from .redis import build_redis_client, ping

__all__ = [
    "IdempotencyGuard",
    "IdempotencyOutcome",
    "IdempotencyStatus",
    "InMemoryIdempotencyGuard",
    "InMemoryRateLimiter",
    "RateLimiterBackend",
    "RedisIdempotencyGuard",
    "RedisLock",
    "RedisRateLimiter",
    "build_redis_client",
    "digest",
    "make_key",
    "ping",
    "redis_lock",
]
