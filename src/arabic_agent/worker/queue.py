"""Asynchronous job queue.

The API process must not own long-running agent execution. When a run is
created it is persisted and a *job* is enqueued; a separate worker process
consumes jobs and executes the agent. This decouples request latency from task
duration, lets workers scale independently, and — crucially — means a killed
worker does not lose the task, because the durable run row and the queue entry
survive it.

Two implementations satisfy the same :class:`JobQueue` contract:

* :class:`InMemoryJobQueue` — a local, single-process queue for the
  offline/desktop runtime and tests. No Redis required.
* :class:`RedisJobQueue` — a Redis-backed queue using a reliable-processing
  pattern (a ready list plus an in-flight sorted set), so a job whose worker
  dies is reclaimed by another worker rather than being lost.

Why Redis rather than a heavier broker: it is already required for distributed
rate limiting and idempotency, its operational cost is low, and a reliable queue
is a few dozen lines on top of primitives we need anyway. Introducing
Kafka/Celery would add infrastructure the project does not yet need.
"""

from __future__ import annotations

import json
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

import redis.asyncio as aioredis
from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import StrEnum
from ..core.errors import ValidationError
from ..core.ids import PREFIX_JOB, new_id
from ..infrastructure.redis import eval_script


class JobType(StrEnum):
    """Kind of work a job represents."""

    START_RUN = "start_run"
    RESUME_RUN = "resume_run"
    RECOVER_RUN = "recover_run"


class Job(BaseModel):
    """A unit of queued work.

    ``payload`` is deliberately opaque JSON validated by the worker; a job
    carries identifiers, not secrets, so a tampered payload cannot smuggle
    credentials into execution.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_JOB))
    type: JobType
    run_id: str
    tenant_id: str
    user_id: str
    #: Delivery attempts so far (incremented on each claim).
    attempts: int = 0
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = Field(default_factory=dict)

    def encode(self) -> str:
        return self.model_dump_json()

    @classmethod
    def decode(cls, raw: str | bytes) -> Job:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError("malformed job payload") from exc
        try:
            return cls.model_validate(data)
        except Exception as exc:
            raise ValidationError("invalid job payload") from exc


@dataclass
class ClaimedJob:
    """A job handed to a worker, with a handle used to ack/nack it."""

    job: Job
    handle: str


class JobQueue(ABC):
    """Contract for a reliable job queue."""

    @abstractmethod
    async def enqueue(self, job: Job) -> Job: ...

    @abstractmethod
    async def claim(self, *, timeout_seconds: float = 1.0) -> ClaimedJob | None:
        """Claim the next job, or return ``None`` if the queue is empty."""

    @abstractmethod
    async def ack(self, claimed: ClaimedJob) -> None:
        """Mark a job as successfully processed."""

    @abstractmethod
    async def nack(self, claimed: ClaimedJob, *, requeue: bool) -> None:
        """Return a job: ``requeue`` for retry, else drop it."""

    @abstractmethod
    async def reclaim_stale(self, *, older_than_seconds: float) -> int:
        """Requeue jobs whose worker died before acking; return how many."""

    @abstractmethod
    async def depth(self) -> int:
        """Approximate number of pending jobs (for backpressure/metrics)."""

    async def aclose(self) -> None:
        return None


@dataclass
class _Entry:
    job: Job
    claimed_at: float


@dataclass
class InMemoryJobQueue(JobQueue):
    """Process-local queue. Not shared across processes (local runtime/tests)."""

    _ready: list[Job] = field(default_factory=list)
    _inflight: dict[str, _Entry] = field(default_factory=dict)

    async def enqueue(self, job: Job) -> Job:
        self._ready.append(job)
        return job

    async def claim(self, *, timeout_seconds: float = 1.0) -> ClaimedJob | None:
        if not self._ready:
            return None
        job = self._ready.pop(0)
        handle = uuid.uuid4().hex
        claimed = job.model_copy(update={"attempts": job.attempts + 1})
        self._inflight[handle] = _Entry(job=claimed, claimed_at=time.monotonic())
        return ClaimedJob(job=claimed, handle=handle)

    async def ack(self, claimed: ClaimedJob) -> None:
        self._inflight.pop(claimed.handle, None)

    async def nack(self, claimed: ClaimedJob, *, requeue: bool) -> None:
        entry = self._inflight.pop(claimed.handle, None)
        if entry is not None and requeue:
            self._ready.append(entry.job)

    async def reclaim_stale(self, *, older_than_seconds: float) -> int:
        now = time.monotonic()
        stale = [
            handle
            for handle, entry in self._inflight.items()
            if now - entry.claimed_at >= older_than_seconds
        ]
        for handle in stale:
            entry = self._inflight.pop(handle)
            self._ready.append(entry.job)
        return len(stale)

    async def depth(self) -> int:
        return len(self._ready)


#: Atomically move one item from the ready list into the in-flight sorted set.
#: Returns the encoded job, or false when the ready list is empty.
_CLAIM_LUA = """
local ready = KEYS[1]
local inflight = KEYS[2]
local now = tonumber(ARGV[1])
local raw = redis.call('RPOP', ready)
if not raw then
  return false
end
redis.call('ZADD', inflight, now, raw)
return raw
"""

#: Move jobs whose claim is older than the cutoff back to the ready list.
_RECLAIM_LUA = """
local ready = KEYS[1]
local inflight = KEYS[2]
local cutoff = tonumber(ARGV[1])
local members = redis.call('ZRANGEBYSCORE', inflight, '-inf', cutoff, 'LIMIT', 0, 100)
local count = 0
for _, m in ipairs(members) do
  redis.call('ZREM', inflight, m)
  redis.call('LPUSH', ready, m)
  count = count + 1
end
return count
"""


class RedisJobQueue(JobQueue):
    """Redis-backed reliable queue.

    ``<prefix>:ready`` is a list of encoded jobs; ``<prefix>:inflight`` is a
    sorted set of in-flight jobs scored by claim time. Claiming moves an item
    from ready to inflight atomically; ack removes it; nack re-pushes it.
    """

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        prefix: str = "aap:jobs",
        visibility_timeout_seconds: float = 300.0,
    ) -> None:
        self._redis = client
        self._prefix = prefix
        self._visibility = visibility_timeout_seconds

    @property
    def _ready_key(self) -> str:
        return f"{self._prefix}:ready"

    @property
    def _inflight_key(self) -> str:
        return f"{self._prefix}:inflight"

    async def enqueue(self, job: Job) -> Job:
        await cast("Awaitable[int]", self._redis.lpush(self._ready_key, job.encode()))
        return job

    async def claim(self, *, timeout_seconds: float = 1.0) -> ClaimedJob | None:
        raw = await eval_script(
            self._redis, _CLAIM_LUA, 2, self._ready_key, self._inflight_key, time.time()
        )
        if not raw:
            return None
        encoded = raw if isinstance(raw, str) else raw.decode("utf-8")
        job = Job.decode(encoded).model_copy(update={"attempts": _attempts_of(encoded) + 1})
        # Re-store with the incremented attempt count so a requeue carries it.
        await cast("Awaitable[int]", self._redis.zrem(self._inflight_key, encoded))
        new_encoded = job.encode()
        await cast(
            "Awaitable[int]", self._redis.zadd(self._inflight_key, {new_encoded: time.time()})
        )
        return ClaimedJob(job=job, handle=new_encoded)

    async def ack(self, claimed: ClaimedJob) -> None:
        await cast("Awaitable[int]", self._redis.zrem(self._inflight_key, claimed.handle))

    async def nack(self, claimed: ClaimedJob, *, requeue: bool) -> None:
        await cast("Awaitable[int]", self._redis.zrem(self._inflight_key, claimed.handle))
        if requeue:
            await cast("Awaitable[int]", self._redis.lpush(self._ready_key, claimed.job.encode()))

    async def reclaim_stale(self, *, older_than_seconds: float) -> int:
        cutoff = time.time() - older_than_seconds
        result = await eval_script(
            self._redis, _RECLAIM_LUA, 2, self._ready_key, self._inflight_key, cutoff
        )
        return int(result)

    async def depth(self) -> int:
        return int(await cast("Awaitable[int]", self._redis.llen(self._ready_key)))


def _attempts_of(encoded: str) -> int:
    try:
        return int(json.loads(encoded).get("attempts", 0))
    except (json.JSONDecodeError, TypeError, ValueError):
        return 0
