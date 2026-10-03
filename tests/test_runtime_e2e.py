"""End-to-end durable runtime test.

This is the Phase 2 acceptance test: a run is created through the real
composition root (Postgres persistence + Redis queue), enqueued rather than
executed inline, then executed by a worker and read back from the database.
Nothing is mocked except the model provider; the queue, stores and worker are
the production implementations.
"""

from __future__ import annotations

import pytest

from arabic_agent.api.service import RunService
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import RunStatus
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.permissions.base import Permission, Principal
from arabic_agent.worker.queue import RedisJobQueue
from arabic_agent.worker.runtime import Worker

from .conftest import TEST_DATABASE_URL, TEST_REDIS_URL
from .helpers import FakeProvider, plan_json, scripted_responder, verify_json

pytestmark = [
    pytest.mark.skipif(
        not (TEST_DATABASE_URL and TEST_REDIS_URL),
        reason="requires AAP_TEST_DATABASE_URL and AAP_TEST_REDIS_URL",
    ),
    pytest.mark.postgres,
    pytest.mark.redis,
]


@pytest.fixture()
async def wired(postgres_database, redis_client, tmp_path):
    """A queued-mode platform sharing the Postgres schema and Redis test DB."""
    settings = Settings(
        environment="test",
        require_auth=False,
        local_store_dir=str(tmp_path / "data"),
        execution_mode="queued",
        worker_lease_seconds=30,
        job_max_attempts=3,
    )
    respond = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )
    registry = ProviderRegistry()
    registry.register(FakeProvider(respond))
    platform = Platform.build(
        settings,
        provider_registry=registry,
        default_model="fake/model",
        database=postgres_database,
        redis_client=redis_client,
    )
    # The Postgres schema and Redis test DB are owned by the fixtures; the
    # platform shares them but the fixtures dispose them at teardown.
    yield platform


async def test_queued_run_is_persisted_then_executed_by_worker(wired, tmp_path):
    principal = Principal(
        tenant_id="tnt_1",
        user_id="usr_1",
        roles=frozenset({"admin"}),
        granted_permissions=frozenset({Permission.READ_FILE, Permission.RUN_COMMAND}),
    )
    service = RunService(wired, settings=wired.settings)

    created = await service.start(
        principal=principal, goal="جهز تقرير", workspace_dir=str(tmp_path), idempotency_key="k1"
    )
    # Enqueued, not executed: the run is pending and the queue is non-empty.
    assert created.status is RunStatus.PENDING
    assert await wired.queue.depth() == 1

    # Re-issuing with the same idempotency key returns the same run, no duplicate.
    again = await service.start(
        principal=principal, goal="جهز تقرير", workspace_dir=str(tmp_path), idempotency_key="k1"
    )
    assert again.id == created.id
    assert await wired.queue.depth() == 1

    worker = Worker(platform=wired, runs=wired.runs, queue=wired.queue, settings=wired.settings)
    assert await worker.process_one() is True

    final = await service.get(created.id, tenant_id="tnt_1")
    assert final.status is RunStatus.COMPLETED
    assert final.verified is True
    assert final.workspace_dir == created.workspace_dir
    # The queue accepted the job and the worker acked it.
    assert await wired.queue.depth() == 0


async def test_queued_run_survives_worker_loss(wired, tmp_path):
    """A run whose worker never executes it is reclaimed by the recovery sweep."""
    from datetime import UTC, datetime, timedelta

    principal = Principal(tenant_id="tnt_1", user_id="usr_1", roles=frozenset({"admin"}))
    service = RunService(wired, settings=wired.settings)
    created = await service.start(principal=principal, goal="استأنف", workspace_dir=str(tmp_path))

    # Simulate a worker that claimed the run then died: lease expired.
    record = await wired.runs.get(created.id, tenant_id="tnt_1")
    dead = record.transition(
        RunStatus.RUNNING,
        worker_id="dead-worker",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=10),
    )
    await wired.runs.update(dead, expected_version=record.version)
    # Drain the original job so recovery is the only path forward.
    claimed = await wired.queue.claim()
    assert claimed is not None

    worker = Worker(platform=wired, runs=wired.runs, queue=wired.queue, settings=wired.settings)
    assert await worker.recover_stale() == 1
    assert await worker.process_one() is True

    final = await service.get(created.id, tenant_id="tnt_1")
    assert final.status is RunStatus.COMPLETED
    assert final.recovery_attempts == 1


def test_redis_queue_class_is_used(wired):
    assert isinstance(wired.queue, RedisJobQueue)
    assert wired.durable is True
    assert wired.distributed is True
