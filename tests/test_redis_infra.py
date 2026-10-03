"""Redis infrastructure integration tests.

Run against a real Redis (never a mock) and skipped unless
``AAP_TEST_REDIS_URL`` is set. They cover the reliability primitives the
production runtime depends on: token-bucket rate limiting, distributed locks,
the idempotency guard, and the reliable job queue's claim/ack/nack/reclaim
lifecycle.
"""

from __future__ import annotations

import asyncio

import pytest

from arabic_agent.infrastructure.idempotency import (
    IdempotencyOutcome,
    RedisIdempotencyGuard,
)
from arabic_agent.infrastructure.lock import RedisLock
from arabic_agent.infrastructure.rate_limit import RedisRateLimiter
from arabic_agent.worker.queue import Job, JobType, RedisJobQueue

from .conftest import requires_redis

pytestmark = [requires_redis, pytest.mark.redis]


def _job(**overrides) -> Job:
    base: dict = {
        "type": JobType.START_RUN,
        "run_id": "run_1",
        "tenant_id": "tnt_1",
        "user_id": "usr_1",
    }
    base.update(overrides)
    return Job(**base)


async def test_rate_limiter_enforces_bucket(redis_client):
    limiter = RedisRateLimiter(redis_client, rate_per_minute=60, burst=2)
    assert await limiter.allow("tenant-a") is True
    assert await limiter.allow("tenant-a") is True
    assert await limiter.allow("tenant-a") is False
    # A different key has its own bucket.
    assert await limiter.allow("tenant-b") is True
    # The exhausted bucket reports a positive wait; a fresh bucket does not.
    assert await limiter.retry_after_seconds("tenant-a") > 0
    assert await limiter.retry_after_seconds("tenant-b") == 0.0


async def test_lock_is_mutually_exclusive(redis_client):
    first = RedisLock(redis_client, "resource", token="tok-a", ttl_seconds=5)
    second = RedisLock(redis_client, "resource", token="tok-b", ttl_seconds=5)
    assert await first.acquire() is True
    assert await second.acquire() is False
    await first.release()
    assert await second.acquire() is True
    await second.release()


async def test_idempotency_guard_lifecycle(redis_client):
    guard = RedisIdempotencyGuard(redis_client)
    first = await guard.begin(key="op-1", fingerprint="fp", ttl_seconds=60)
    assert first.outcome == IdempotencyOutcome.ACQUIRED
    # A concurrent duplicate is rejected while in flight.
    second = await guard.begin(key="op-1", fingerprint="fp", ttl_seconds=60)
    assert second.outcome == IdempotencyOutcome.IN_FLIGHT
    await guard.complete(key="op-1", result={"ok": True}, ttl_seconds=60)
    third = await guard.begin(key="op-1", fingerprint="fp", ttl_seconds=60)
    assert third.outcome == IdempotencyOutcome.COMPLETED
    assert third.result == {"ok": True}


async def test_queue_claim_ack(redis_client):
    queue = RedisJobQueue(redis_client)
    await queue.enqueue(_job())
    assert await queue.depth() == 1
    claimed = await queue.claim()
    assert claimed is not None
    assert claimed.job.attempts == 1
    await queue.ack(claimed)
    assert await queue.claim() is None


async def test_queue_nack_requeues(redis_client):
    queue = RedisJobQueue(redis_client)
    await queue.enqueue(_job())
    claimed = await queue.claim()
    assert claimed is not None
    await queue.nack(claimed, requeue=True)
    again = await queue.claim()
    assert again is not None
    assert again.job.attempts == 2  # the requeue carried the attempt count


async def test_queue_nack_drops_when_not_requeued(redis_client):
    queue = RedisJobQueue(redis_client)
    await queue.enqueue(_job())
    claimed = await queue.claim()
    assert claimed is not None
    await queue.nack(claimed, requeue=False)
    assert await queue.claim() is None


async def test_queue_reclaims_stale_inflight(redis_client):
    queue = RedisJobQueue(redis_client)
    await queue.enqueue(_job())
    claimed = await queue.claim()
    assert claimed is not None
    # Nothing is stale yet.
    assert await queue.reclaim_stale(older_than_seconds=300) == 0
    # After the visibility window, the in-flight job is requeued.
    await asyncio.sleep(0.05)
    assert await queue.reclaim_stale(older_than_seconds=0) == 1
    assert await queue.claim() is not None
