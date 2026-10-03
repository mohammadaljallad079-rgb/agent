"""Rate limiter contract, in-process and Redis-backed implementations.

The in-process limiter is correct for a single node (local runtime, tests). The
Redis limiter enforces a single shared budget across all API replicas: it runs a
token-bucket refill atomically in Lua so concurrent requests cannot overspend.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import redis.asyncio as aioredis

from .redis import eval_script


class RateLimiterBackend(ABC):
    """Contract for a token-bucket rate limiter."""

    @abstractmethod
    async def allow(self, key: str) -> bool:
        """Consume one token for ``key``; return whether the call is allowed."""

    @abstractmethod
    async def retry_after_seconds(self, key: str) -> float:
        """Seconds until the next token is available for ``key``."""

    async def aclose(self) -> None:
        return None


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


@dataclass
class InMemoryRateLimiter(RateLimiterBackend):
    """Process-local token bucket (single node)."""

    rate_per_minute: int
    burst: int = 0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.burst <= 0:
            self.burst = max(1, self.rate_per_minute)
        self._refill_rate = self.rate_per_minute / 60.0

    async def allow(self, key: str) -> bool:
        now = time.monotonic()
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

    async def retry_after_seconds(self, key: str) -> float:
        bucket = self._buckets.get(key)
        if bucket is None or bucket.tokens >= 1.0:
            return 0.0
        needed = 1.0 - bucket.tokens
        return round(needed / self._refill_rate, 2) if self._refill_rate else 60.0


#: Atomic token-bucket refill + consume. KEYS[1]=bucket hash, ARGV =
#: rate_per_minute, burst, now_seconds, cost.
_ALLOW_LUA = """
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local data = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = burst
  ts = now
end
local refill = (now - ts) * (rate / 60.0)
tokens = math.min(burst, tokens + refill)
local allowed = 0
if tokens >= cost then
  tokens = tokens - cost
  allowed = 1
end
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', KEYS[1], 120)
-- Report the time until a single token is available, independent of ``cost``,
-- so a cost=0 probe (used by retry_after_seconds) returns a useful wait.
local wait = 0
if tokens < 1 then
  wait = (1 - tokens) / (rate / 60.0)
end
-- Redis truncates Lua numbers returned to integers, which would turn any
-- sub-second wait into 0; return the wait as a string to preserve precision.
return {allowed, tostring(wait)}
"""


class RedisRateLimiter(RateLimiterBackend):
    """Distributed token-bucket limiter shared by all API replicas."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        rate_per_minute: int,
        burst: int = 0,
        prefix: str = "aap:ratelimit",
    ) -> None:
        self._redis = client
        self._rate = rate_per_minute
        self._burst = burst if burst > 0 else max(1, rate_per_minute)
        self._prefix = prefix

    async def allow(self, key: str) -> bool:
        allowed, _ = await self._consume(key)
        return allowed

    async def retry_after_seconds(self, key: str) -> float:
        _, wait = await self._consume(key, cost=0)
        return round(wait, 2)

    async def _consume(self, key: str, *, cost: int = 1) -> tuple[bool, float]:
        result = await eval_script(
            self._redis,
            _ALLOW_LUA,
            1,
            f"{self._prefix}:{key}",
            self._rate,
            self._burst,
            time.time(),
            cost,
        )
        return bool(result[0]), float(result[1])
