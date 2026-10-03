"""Idempotency guard.

Protects side-effecting operations from being performed twice when a request is
retried, a job is delivered twice, or a worker crashes after acting but before
recording success. Two outcomes are possible:

* ``ACQUIRED``  — this is the first attempt; the caller may perform the effect.
* ``IN_FLIGHT`` — an identical attempt is currently running elsewhere.
* ``COMPLETED`` — the effect already succeeded; the caller must not repeat it.

The guard stores only a digest of the request (never raw arguments, which may
contain secrets) and a small JSON result. Redis is ephemeral by design: if the
cache is lost the worst case is a duplicate attempt, which the durable audit log
makes detectable. Durable business idempotency (e.g. a unique invoice number)
remains the integration's responsibility.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import StrEnum
from ..core.redaction import redact
from .redis import eval_script


class IdempotencyOutcome(StrEnum):
    ACQUIRED = "acquired"
    IN_FLIGHT = "in_flight"
    COMPLETED = "completed"


@dataclass(frozen=True)
class IdempotencyStatus:
    outcome: str
    result: dict[str, Any] | None = None


class IdempotencyRecord(BaseModel):
    """The cached outcome of a completed idempotent operation."""

    model_config = ConfigDict(frozen=True)

    key: str
    result: dict[str, Any] = Field(default_factory=dict)


def digest(payload: dict[str, Any]) -> str:
    """Return a stable digest of a request payload (sensitive values masked)."""
    canonical = json.dumps(redact(payload), sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def make_key(*, tenant_id: str, scope: str, key: str) -> str:
    return f"aap:idem:{tenant_id}:{scope}:{key}"


class IdempotencyGuard(ABC):
    """Contract for an idempotency guard."""

    @abstractmethod
    async def begin(self, *, key: str, fingerprint: str, ttl_seconds: int) -> IdempotencyStatus:
        """Claim ``key`` for the given fingerprint."""

    @abstractmethod
    async def complete(self, *, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        """Record the successful result so a retry can replay it."""

    @abstractmethod
    async def abort(self, *, key: str) -> None:
        """Release a claim after a failure so a retry may proceed."""

    async def aclose(self) -> None:
        return None


#: Atomically claim a key: 1 = acquired, 0 = already present.
_CLAIM_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
return 1
"""


class RedisIdempotencyGuard(IdempotencyGuard):
    """Redis-backed guard shared across API and worker processes."""

    def __init__(self, client: aioredis.Redis, *, prefix: str = "aap:idem") -> None:
        self._redis = client
        self._prefix = prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    async def begin(self, *, key: str, fingerprint: str, ttl_seconds: int) -> IdempotencyStatus:
        claimed = await eval_script(
            self._redis, _CLAIM_LUA, 1, self._k(key), f"in_flight:{fingerprint}", ttl_seconds
        )
        if claimed:
            return IdempotencyStatus(outcome=IdempotencyOutcome.ACQUIRED)
        stored = await self._redis.get(self._k(key))
        if stored is None:
            # Expired between the claim and the read; retry the claim once.
            claimed = await eval_script(
                self._redis, _CLAIM_LUA, 1, self._k(key), f"in_flight:{fingerprint}", ttl_seconds
            )
            if claimed:
                return IdempotencyStatus(outcome=IdempotencyOutcome.ACQUIRED)
            stored = await self._redis.get(self._k(key))
        if stored and stored.startswith("done:"):
            return IdempotencyStatus(
                outcome=IdempotencyOutcome.COMPLETED,
                result=json.loads(stored[len("done:") :]),
            )
        return IdempotencyStatus(outcome=IdempotencyOutcome.IN_FLIGHT)

    async def complete(self, *, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        payload = json.dumps(redact(result), ensure_ascii=False, default=str)
        await self._redis.set(self._k(key), f"done:{payload}", ex=ttl_seconds)

    async def abort(self, *, key: str) -> None:
        await self._redis.delete(self._k(key))


class InMemoryIdempotencyGuard(IdempotencyGuard):
    """Process-local guard for the local/offline runtime and tests."""

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    async def begin(self, *, key: str, fingerprint: str, ttl_seconds: int) -> IdempotencyStatus:
        existing = self._items.get(key)
        if existing is None:
            self._items[key] = f"in_flight:{fingerprint}"
            return IdempotencyStatus(outcome=IdempotencyOutcome.ACQUIRED)
        if existing.startswith("done:"):
            return IdempotencyStatus(
                outcome=IdempotencyOutcome.COMPLETED,
                result=json.loads(existing[len("done:") :]),
            )
        return IdempotencyStatus(outcome=IdempotencyOutcome.IN_FLIGHT)

    async def complete(self, *, key: str, result: dict[str, Any], ttl_seconds: int) -> None:
        payload = json.dumps(redact(result), ensure_ascii=False, default=str)
        self._items[key] = f"done:{payload}"

    async def abort(self, *, key: str) -> None:
        self._items.pop(key, None)
