"""Redis client construction.

Redis is ephemeral coordination only — rate limiting, the job queue, short-lived
locks and the idempotency cache. It is never the sole home for important state;
Postgres is the durable source of truth. A local/offline runtime may run with no
Redis at all.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable
from typing import Any, cast

import redis.asyncio as aioredis

from ..core.errors import ConfigurationError

logger = logging.getLogger(__name__)


def build_redis_client(url: str, *, decode_responses: bool = True) -> aioredis.Redis:
    """Create a Redis client from a URL (``redis://host:port/db``).

    The URL is a ``SecretStr`` at the settings boundary and is never logged.
    """
    if not url:
        raise ConfigurationError("redis URL is required to build a Redis client")
    return cast(
        aioredis.Redis,
        aioredis.from_url(url, decode_responses=decode_responses),  # type: ignore[no-untyped-call]
    )


async def eval_script(
    client: aioredis.Redis,
    script: str,
    num_keys: int,
    *args: str | int | float,
) -> Any:
    """Run a Lua script, coercing numeric arguments to the strings Redis expects.

    redis-py's type stubs require string arguments for ``EVAL`` even though
    Redis accepts numbers; this wrapper keeps call sites readable and typed.
    """
    pending = client.eval(script, num_keys, *[str(a) for a in args])
    return await cast("Awaitable[Any]", pending)


async def ping(client: aioredis.Redis) -> bool:
    """Return whether the Redis server is reachable."""
    try:
        return bool(await client.ping())
    except Exception:
        logger.warning("redis ping failed", exc_info=True)
        return False
