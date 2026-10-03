"""Readiness probe integration tests against real Postgres and Redis.

Opt-in: skipped unless ``AAP_TEST_DATABASE_URL`` / ``AAP_TEST_REDIS_URL`` are
set. They prove the probes actually reach the live dependency (not a mock) and
that a broken dependency flips readiness to not-ready.
"""

from __future__ import annotations

import pytest

from arabic_agent.observability.readiness import (
    DatabaseProbe,
    ReadinessService,
    RedisProbe,
)

from .conftest import requires_postgres, requires_redis

pytestmark = [pytest.mark.postgres, pytest.mark.redis]


@requires_postgres
async def test_database_probe_ok(postgres_database):
    service = ReadinessService([DatabaseProbe(postgres_database)])
    report = await service.check()
    assert report.ready is True
    assert report.results[0].name == "database"
    assert report.results[0].ok is True


@requires_postgres
async def test_database_probe_reports_unreachable():
    from arabic_agent.persistence import Database

    # A database pointing at a closed port: the probe must report not-ready
    # without leaking the connection string.
    dead = Database("postgresql+asyncpg://aap:secret@127.0.0.1:1/aap")
    try:
        service = ReadinessService([DatabaseProbe(dead)], timeout_seconds=2.0)
        report = await service.check()
    finally:
        await dead.dispose()
    assert report.ready is False
    assert report.results[0].ok is False
    assert "secret" not in report.results[0].detail


@requires_redis
async def test_redis_probe_ok(redis_client):
    service = ReadinessService([RedisProbe(redis_client)])
    report = await service.check()
    assert report.ready is True
    assert report.results[0].name == "redis"
    assert report.results[0].ok is True
