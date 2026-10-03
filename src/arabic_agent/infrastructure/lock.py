"""Short-lived distributed locks.

Used to guarantee that at most one worker processes a given run at a time. Locks
are advisory and always carry a TTL, so a crashed holder cannot deadlock the
system — the lease simply expires.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis

from .redis import eval_script

#: Release only if we still hold the lock (compare-and-delete).
_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RedisLock:
    """A single-holder lock with a TTL, backed by Redis ``SET NX PX``."""

    def __init__(
        self,
        client: aioredis.Redis,
        key: str,
        *,
        token: str,
        ttl_seconds: int,
    ) -> None:
        self._redis = client
        self._key = key
        self._token = token
        self._ttl_ms = ttl_seconds * 1000

    async def acquire(self) -> bool:
        return bool(await self._redis.set(self._key, self._token, nx=True, px=self._ttl_ms))

    async def release(self) -> None:
        await eval_script(self._redis, _RELEASE_LUA, 1, self._key, self._token)


@asynccontextmanager
async def redis_lock(
    client: aioredis.Redis,
    key: str,
    *,
    token: str,
    ttl_seconds: int,
) -> AsyncIterator[bool]:
    """Acquire ``key``; yield whether the lock was obtained, and release if so."""
    lock = RedisLock(client, key, token=token, ttl_seconds=ttl_seconds)
    acquired = await lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            await lock.release()
