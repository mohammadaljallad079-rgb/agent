"""Worker runtime and recovery tests.

These exercise the real :class:`Worker` / :class:`RunExecutor` against in-memory
stores (no mocks of the code under test). They prove the properties that make
the runtime recoverable: a queued run is executed by a worker, a run whose
worker lease lapsed is reclaimed and re-run, a run paused for approval is left
untouched, and a poison job eventually fails the run instead of looping.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import RunStatus
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.runs.base import RunRecord
from arabic_agent.runs.store import InMemoryRunStore
from arabic_agent.worker.queue import InMemoryJobQueue, Job, JobType
from arabic_agent.worker.runtime import Worker

from .helpers import FakeProvider, Responder, plan_json, scripted_responder, verify_json


def _build(
    settings: Settings, respond: Responder
) -> tuple[Platform, InMemoryRunStore, InMemoryJobQueue]:
    registry = ProviderRegistry()
    registry.register(FakeProvider(respond))
    runs = InMemoryRunStore()
    queue = InMemoryJobQueue()
    platform = Platform.build(
        settings,
        provider_registry=registry,
        default_model="fake/model",
        run_store=runs,
        queue=queue,
    )
    return platform, runs, queue


def _worker(
    platform: Platform,
    runs: InMemoryRunStore,
    queue: InMemoryJobQueue,
    settings: Settings,
) -> Worker:
    return Worker(platform=platform, runs=runs, queue=queue, settings=settings)


def _responder() -> Responder:
    return scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )


async def test_worker_executes_queued_run(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="قل مرحبا", workspace_dir=str(tmp_path))
    )
    await queue.enqueue(Job(type=JobType.START_RUN, run_id=record.id, tenant_id="t1", user_id="u1"))

    worker = _worker(platform, runs, queue, settings)
    assert await worker.process_one() is True

    final = await runs.get(record.id, tenant_id="t1")
    assert final.status is RunStatus.COMPLETED
    assert final.verified is True
    assert final.lease_expires_at is None
    assert await queue.depth() == 0


async def test_worker_recovers_stale_run(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="استأنف", workspace_dir=str(tmp_path))
    )
    # Simulate a worker that died mid-run: RUNNING with an expired lease and no
    # job left in the queue.
    dead = record.transition(
        RunStatus.RUNNING,
        worker_id="worker-dead",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    await runs.update(dead, expected_version=record.version)

    worker = _worker(platform, runs, queue, settings)
    assert await worker.recover_stale() == 1

    reclaimed = await runs.get(record.id, tenant_id="t1")
    assert reclaimed.recovery_attempts == 1
    assert reclaimed.lease_expires_at is not None

    # The recovery loop enqueued a RECOVER_RUN job; the worker executes it and
    # the run finishes rather than being lost.
    assert await worker.process_one() is True
    final = await runs.get(record.id, tenant_id="t1")
    assert final.status is RunStatus.COMPLETED


async def test_recovery_leaves_waiting_approval_untouched(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="انتظر", workspace_dir=str(tmp_path))
    )
    waiting = record.transition(RunStatus.RUNNING).transition(RunStatus.WAITING_APPROVAL)
    await runs.update(waiting, expected_version=record.version)

    worker = _worker(platform, runs, queue, settings)
    assert await worker.recover_stale() == 0

    await queue.enqueue(Job(type=JobType.START_RUN, run_id=record.id, tenant_id="t1", user_id="u1"))
    assert await worker.process_one() is True  # job is acked but run is not executed
    final = await runs.get(record.id, tenant_id="t1")
    assert final.status is RunStatus.WAITING_APPROVAL


async def test_recovery_gives_up_after_max_attempts(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="فشل", workspace_dir=str(tmp_path))
    )
    stuck = record.transition(RunStatus.RUNNING).model_copy(
        update={
            "recovery_attempts": settings.job_max_attempts,
            "lease_expires_at": datetime.now(UTC) - timedelta(seconds=30),
        }
    )
    await runs.update(stuck, expected_version=record.version)

    worker = _worker(platform, runs, queue, settings)
    assert await worker.recover_stale() == 0
    final = await runs.get(record.id, tenant_id="t1")
    assert final.status is RunStatus.FAILED
    assert final.error == "exceeded recovery attempts"


async def test_poison_job_fails_run_after_max_attempts(settings: Settings, tmp_path, monkeypatch):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="سم", workspace_dir=str(tmp_path))
    )
    await runs.update(record.transition(RunStatus.RUNNING), expected_version=record.version)

    worker = _worker(platform, runs, queue, settings)

    async def explode(_job: Job) -> None:
        raise RuntimeError("dispatch exploded")

    monkeypatch.setattr(worker, "_dispatch_job", explode)

    await queue.enqueue(Job(type=JobType.START_RUN, run_id=record.id, tenant_id="t1", user_id="u1"))
    # Each claim increments attempts; after max attempts the run is failed.
    for _ in range(settings.job_max_attempts):
        assert await worker.process_one() is True

    final = await runs.get(record.id, tenant_id="t1")
    assert final.status is RunStatus.FAILED
    assert "max attempts" in (final.error or "")


async def test_worker_records_run_and_job_metrics(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="قل مرحبا", workspace_dir=str(tmp_path))
    )
    await queue.enqueue(Job(type=JobType.START_RUN, run_id=record.id, tenant_id="t1", user_id="u1"))

    worker = _worker(platform, runs, queue, settings)
    await worker.process_one()

    registry = platform.metrics.registry
    assert registry.counter_value("aap_jobs_claimed_total", labels={"type": "start_run"}) == 1
    assert registry.counter_value("aap_jobs_acked_total", labels={"type": "start_run"}) == 1
    assert registry.counter_value("aap_runs_finished_total", labels={"status": "completed"}) == 1
    assert registry.histogram_count("aap_run_duration_seconds", labels={"status": "completed"}) == 1


async def test_recovery_records_recovery_metric(settings: Settings, tmp_path):
    platform, runs, queue = _build(settings, _responder())
    record = await runs.create(
        RunRecord(tenant_id="t1", user_id="u1", goal="استأنف", workspace_dir=str(tmp_path))
    )
    dead = record.transition(
        RunStatus.RUNNING,
        worker_id="worker-dead",
        lease_expires_at=datetime.now(UTC) - timedelta(seconds=30),
    )
    await runs.update(dead, expected_version=record.version)

    worker = _worker(platform, runs, queue, settings)
    assert await worker.recover_stale() == 1
    assert platform.metrics.registry.counter_value("aap_runs_recovered_total") == 1
