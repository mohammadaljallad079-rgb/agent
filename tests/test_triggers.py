"""Trigger and scheduler tests.

These exercise the real trigger service, real stores, real policy engine and the
real workflow engine. Only the LLM is the deterministic fake. The point is to
prove that a trigger firing becomes exactly one durable workflow run, that the
scheduler claims occurrences atomically and durably, and that every trigger kind
(manual, cron, webhook, event) converges on the same execution path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from arabic_agent.api.workflow_service import WorkflowService
from arabic_agent.core.enums import MisfirePolicy, StepKind, TriggerKind, WorkflowRunStatus
from arabic_agent.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from arabic_agent.permissions.base import Permission, Principal
from arabic_agent.triggers.cron import load_timezone, validate_cron
from arabic_agent.triggers.scheduler import TriggerScheduler
from arabic_agent.triggers.service import TriggerService
from arabic_agent.triggers.signing import compute_signature, verify_signature
from arabic_agent.worker.queue import InMemoryJobQueue
from arabic_agent.worker.runtime import Worker
from arabic_agent.workflows.models import WorkflowStep

from .helpers import plan_json, scripted_responder


def _principal(**kwargs: Any) -> Principal:
    defaults: dict[str, Any] = {
        "tenant_id": "tnt_1",
        "user_id": "usr_1",
        "roles": frozenset({"owner"}),
        "granted_permissions": frozenset({Permission.MANAGE_WORKFLOW, Permission.RUN_WORKFLOW}),
    }
    defaults.update(kwargs)
    return Principal(**defaults)


def _step(step_id: str, action: str = "set", **kwargs: Any) -> WorkflowStep:
    return WorkflowStep(id=step_id, kind=StepKind.DETERMINISTIC, action=action, **kwargs)


async def _published_workflow(platform, *, steps: list[WorkflowStep] | None = None):
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="wf",
        publish=True,
        steps=steps or [_step("a")],
    )
    return workflow


# ---------------------------------------------------------------------------
# Cron parser
# ---------------------------------------------------------------------------
def test_cron_rejects_malformed_expressions():
    for bad in ["", "* * *", "* * * * * *", "60 * * * *", "* 24 * * *", "a * * * *"]:
        with pytest.raises(ValidationError):
            validate_cron(bad)


def test_cron_next_after_is_strictly_after():
    expr = validate_cron("*/15 * * * *")
    base = datetime(2026, 1, 1, 10, 7, 30, tzinfo=UTC)
    nxt = expr.next_after(base, tz=load_timezone("UTC"))
    assert nxt == datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    # A moment already on a boundary must advance to the next boundary, never
    # return the input (otherwise a scheduler would spin on the same instant).
    on_boundary = datetime(2026, 1, 1, 10, 15, tzinfo=UTC)
    assert expr.next_after(on_boundary, tz=load_timezone("UTC")) == datetime(
        2026, 1, 1, 10, 30, tzinfo=UTC
    )


def test_cron_daily_and_timezone():
    expr = validate_cron("@daily")
    tz = load_timezone("Asia/Riyadh")  # UTC+3, no DST
    base = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    nxt = expr.next_after(base, tz=tz)
    assert nxt.tzinfo is not None
    local = nxt.astimezone(tz)
    assert (local.hour, local.minute) == (0, 0)
    assert local.date() == datetime(2026, 3, 2, tzinfo=tz).date()


def test_cron_day_of_week_matches():
    expr = validate_cron("0 9 * * 1")  # Mondays 09:00
    monday = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert expr.matches(monday)
    assert not expr.matches(datetime(2026, 1, 6, 9, 0, tzinfo=UTC))


# ---------------------------------------------------------------------------
# Webhook signing
# ---------------------------------------------------------------------------
def test_webhook_signature_roundtrip_and_rejection():
    secret = "s3cr3t"
    body = b'{"event":"invoice.created"}'
    ts = "1700000000"
    sig = compute_signature(secret, timestamp=ts, body=body)
    assert verify_signature(
        secret, timestamp=ts, body=body, signature=sig, tolerance_seconds=300, now=1700000000
    )
    # Tampered body, wrong secret, and stale timestamp all fail closed.
    assert not verify_signature(
        secret, timestamp=ts, body=b"{}", signature=sig, tolerance_seconds=300, now=1700000000
    )
    assert not verify_signature(
        "other", timestamp=ts, body=body, signature=sig, tolerance_seconds=300, now=1700000000
    )
    assert not verify_signature(
        secret, timestamp=ts, body=body, signature=sig, tolerance_seconds=300, now=1700099999
    )
    assert not verify_signature(
        secret, timestamp="not-a-number", body=body, signature=sig, tolerance_seconds=300
    )


# ---------------------------------------------------------------------------
# Trigger authoring + governance
# ---------------------------------------------------------------------------
async def test_create_cron_trigger_sets_next_fire(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, secret = await service.create(
        principal=_principal(),
        name="nightly",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="0 3 * * *",
        timezone="UTC",
    )
    assert secret is None
    assert trigger.next_fire_at is not None
    assert trigger.enabled is True


async def test_create_requires_manage_permission(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    with pytest.raises(PermissionDeniedError):
        await service.create(
            principal=_principal(roles=frozenset(), granted_permissions=frozenset()),
            name="nope",
            workflow_id=workflow.id,
            kind=TriggerKind.MANUAL,
        )


async def test_create_rejects_unpublished_workflow(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow_service = WorkflowService(platform)
    workflow, _ = await workflow_service.create(principal=_principal(), name="draft")
    service = TriggerService(platform)
    with pytest.raises(ConflictError):
        await service.create(
            principal=_principal(),
            name="draft-trigger",
            workflow_id=workflow.id,
            kind=TriggerKind.MANUAL,
        )


async def test_kind_specific_validation(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    with pytest.raises(ValidationError):
        await service.create(
            principal=_principal(),
            name="no-cron",
            workflow_id=workflow.id,
            kind=TriggerKind.CRON,
        )
    with pytest.raises(ValidationError):
        await service.create(
            principal=_principal(),
            name="bad-tz",
            workflow_id=workflow.id,
            kind=TriggerKind.CRON,
            cron="0 3 * * *",
            timezone="Mars/Olympus",
        )


async def test_webhook_secret_returned_once_and_never_on_read(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, secret = await service.create(
        principal=_principal(),
        name="hook",
        workflow_id=workflow.id,
        kind=TriggerKind.WEBHOOK,
    )
    assert secret is not None
    assert trigger.webhook_id is not None
    # A read never exposes the secret.
    fetched = await service.get(trigger.id, tenant_id="tnt_1")
    assert fetched.webhook_secret is not None
    assert "webhook_secret" not in fetched.public_view()


async def test_trigger_is_tenant_scoped(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(), name="mine", workflow_id=workflow.id, kind=TriggerKind.MANUAL
    )
    with pytest.raises(NotFoundError):
        await service.get(trigger.id, tenant_id="tnt_2")


async def test_update_recomputes_next_fire(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="0 3 * * *",
    )
    before = trigger.next_fire_at
    updated = await service.update(principal=_principal(), trigger_id=trigger.id, cron="0 5 * * *")
    assert updated.cron == "0 5 * * *"
    assert updated.next_fire_at != before
    assert updated.next_fire_at.hour == 5


async def test_enable_resumes_from_now(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="0 3 * * *",
    )
    disabled = await service.set_enabled(
        principal=_principal(), trigger_id=trigger.id, enabled=False
    )
    assert disabled.enabled is False
    enabled = await service.set_enabled(principal=_principal(), trigger_id=trigger.id, enabled=True)
    assert enabled.enabled is True
    assert enabled.next_fire_at >= datetime.now(UTC)


# ---------------------------------------------------------------------------
# Firing: every kind → one workflow run
# ---------------------------------------------------------------------------
async def test_manual_fire_starts_workflow_run(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="now",
        workflow_id=workflow.id,
        kind=TriggerKind.MANUAL,
        inputs={"customer": "acme"},
    )
    run = await service.fire_manual(principal=_principal(), trigger_id=trigger.id)
    assert run.workflow_id == workflow.id
    assert run.input["customer"] == "acme"
    assert run.status is WorkflowRunStatus.COMPLETED
    refreshed = await service.get(trigger.id, tenant_id="tnt_1")
    assert refreshed.fire_count == 1
    assert refreshed.last_run_id == run.id


async def test_manual_fire_idempotency_key_prevents_duplicate(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(), name="once", workflow_id=workflow.id, kind=TriggerKind.MANUAL
    )
    first = await service.fire_manual(
        principal=_principal(), trigger_id=trigger.id, idempotency_key="abc"
    )
    second = await service.fire_manual(
        principal=_principal(), trigger_id=trigger.id, idempotency_key="abc"
    )
    assert first.id == second.id


async def test_fire_requires_run_permission(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(), name="now", workflow_id=workflow.id, kind=TriggerKind.MANUAL
    )
    # Creator captured RUN_WORKFLOW, but the *caller* here lacks it.
    with pytest.raises(PermissionDeniedError):
        await service.fire_manual(
            principal=_principal(roles=frozenset(), granted_permissions=frozenset()),
            trigger_id=trigger.id,
        )


async def test_disabled_trigger_does_not_fire(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(), name="now", workflow_id=workflow.id, kind=TriggerKind.MANUAL
    )
    await service.set_enabled(principal=_principal(), trigger_id=trigger.id, enabled=False)
    with pytest.raises(ConflictError):
        await service.fire_manual(principal=_principal(), trigger_id=trigger.id)


async def test_webhook_fire_verifies_signature(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, secret = await service.create(
        principal=_principal(), name="hook", workflow_id=workflow.id, kind=TriggerKind.WEBHOOK
    )
    assert secret is not None and trigger.webhook_id is not None
    body = b'{"hello":"world"}'
    ts = str(int(datetime.now(UTC).timestamp()))
    sig = compute_signature(secret, timestamp=ts, body=body)

    run = await service.fire_webhook(
        webhook_id=trigger.webhook_id, body=body, timestamp=ts, signature=sig, delivery_id="d1"
    )
    assert run.workflow_id == workflow.id

    # A replay of the same delivery id starts the same run (idempotent).
    replay = await service.fire_webhook(
        webhook_id=trigger.webhook_id, body=body, timestamp=ts, signature=sig, delivery_id="d1"
    )
    assert replay.id == run.id

    # A bad signature is rejected outright.
    with pytest.raises(PermissionDeniedError):
        await service.fire_webhook(
            webhook_id=trigger.webhook_id, body=body, timestamp=ts, signature="sha256=bad"
        )


async def test_webhook_unknown_id_is_not_found(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = TriggerService(platform)
    with pytest.raises(NotFoundError):
        await service.fire_webhook(
            webhook_id="whk_missing", body=b"{}", timestamp="1", signature="sha256=x"
        )


async def test_event_fires_only_matching_tenant_and_type(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    await service.create(
        principal=_principal(),
        name="on-invoice",
        workflow_id=workflow.id,
        kind=TriggerKind.EVENT,
        event_type="invoice.created",
    )
    await service.create(
        principal=_principal(),
        name="on-other",
        workflow_id=workflow.id,
        kind=TriggerKind.EVENT,
        event_type="customer.created",
    )
    runs = await service.fire_event(
        event_type="invoice.created", tenant_id="tnt_1", payload={"id": "inv_1"}
    )
    assert len(runs) == 1
    # A different tenant's event never reaches this tenant's triggers.
    assert (
        await service.fire_event(event_type="invoice.created", tenant_id="tnt_2", payload={}) == []
    )


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
def _scheduler(platform, settings) -> TriggerScheduler:
    return TriggerScheduler(
        platform=platform,
        triggers=platform.triggers,
        service=platform.trigger_service,
        settings=settings,
    )


async def test_scheduler_fires_due_cron_and_advances(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="*/5 * * * *",
    )
    # Force the trigger due in the past.
    due = datetime.now(UTC) - timedelta(minutes=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": due}), expected_version=trigger.version
    )

    scheduler = _scheduler(platform, settings)
    fired = await scheduler.run_once()
    assert fired == 1

    refreshed = await service.get(trigger.id, tenant_id="tnt_1")
    assert refreshed.next_fire_at > due
    assert refreshed.fire_count == 1
    runs = await service.list_firings(tenant_id="tnt_1", trigger_id=trigger.id)
    assert len(runs) == 1
    assert runs[0].scheduled_for == due


async def test_scheduler_does_not_double_fire_same_occurrence(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="*/5 * * * *",
    )
    due = datetime.now(UTC) - timedelta(minutes=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": due}), expected_version=trigger.version
    )
    scheduler = _scheduler(platform, settings)
    assert await scheduler.run_once() == 1
    # The occurrence is claimed and next_fire_at advanced, so a second pass has
    # nothing due: exactly-once per occurrence.
    assert await scheduler.run_once() == 0


async def test_scheduler_skip_misfire_policy(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="*/5 * * * *",
        misfire_policy=MisfirePolicy.SKIP,
    )
    # Far outside the grace window.
    stale = datetime.now(UTC) - timedelta(days=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": stale}), expected_version=trigger.version
    )
    scheduler = _scheduler(platform, settings)
    assert await scheduler.run_once() == 0
    refreshed = await service.get(trigger.id, tenant_id="tnt_1")
    assert refreshed.fire_count == 0
    assert refreshed.next_fire_at > stale


async def test_scheduler_run_all_catch_up_is_capped(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="* * * * *",
        misfire_policy=MisfirePolicy.RUN_ALL,
    )
    capped_settings = settings.model_copy(update={"scheduler_max_catchup": 3})
    stale = datetime.now(UTC) - timedelta(hours=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": stale}), expected_version=trigger.version
    )
    scheduler = _scheduler(platform, capped_settings)
    fired = await scheduler.run_once()
    assert fired == 3


async def test_scheduler_ignores_disabled_triggers(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    workflow = await _published_workflow(platform)
    service = TriggerService(platform)
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="*/5 * * * *",
    )
    due = datetime.now(UTC) - timedelta(minutes=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": due, "enabled": False}),
        expected_version=trigger.version,
    )
    scheduler = _scheduler(platform, settings)
    assert await scheduler.run_once() == 0


# ---------------------------------------------------------------------------
# Durable end-to-end: queued trigger firing executed by the worker
# ---------------------------------------------------------------------------
async def test_queued_trigger_firing_is_executed_by_worker(make_platform, settings):
    queued = settings.model_copy(update={"execution_mode": "queued"})
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    platform.settings = queued
    queue = InMemoryJobQueue()
    platform.queue = queue
    workflow_service = WorkflowService(platform, settings=queued)
    workflow, _ = await workflow_service.create(
        principal=_principal(), name="wf", publish=True, steps=[_step("a")]
    )
    service = TriggerService(platform, settings=queued)
    trigger, _ = await service.create(
        principal=_principal(), name="now", workflow_id=workflow.id, kind=TriggerKind.MANUAL
    )
    run = await service.fire_manual(principal=_principal(), trigger_id=trigger.id)
    assert run.status is WorkflowRunStatus.PENDING

    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=queued)
    assert await worker.process_one() is True
    final = await platform.workflow_runs.get_run(run.id, tenant_id="tnt_1")
    assert final.status is WorkflowRunStatus.COMPLETED


async def test_scheduler_fires_trigger_durably_across_restart(make_platform, settings):
    """A firing that starts in queued mode survives a scheduler restart."""
    queued = settings.model_copy(update={"execution_mode": "queued"})
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    platform.settings = queued
    queue = InMemoryJobQueue()
    platform.queue = queue
    workflow_service = WorkflowService(platform, settings=queued)
    workflow, _ = await workflow_service.create(
        principal=_principal(), name="wf", publish=True, steps=[_step("a")]
    )
    service = TriggerService(platform, settings=queued)
    # The scheduler and worker reach the trigger service through the platform,
    # so point the platform at the queued-mode service for this test.
    platform.trigger_service = service
    trigger, _ = await service.create(
        principal=_principal(),
        name="sched",
        workflow_id=workflow.id,
        kind=TriggerKind.CRON,
        cron="*/5 * * * *",
    )
    due = datetime.now(UTC) - timedelta(minutes=1)
    await platform.triggers.update(
        trigger.model_copy(update={"next_fire_at": due}), expected_version=trigger.version
    )
    # First scheduler fires the occurrence and enqueues the run, then "dies".
    scheduler = _scheduler(platform, settings)
    assert await scheduler.run_once() == 1

    # A brand-new scheduler (restart) has nothing to redo: the occurrence was
    # claimed durably, so it must not fire again.
    restarted = _scheduler(platform, settings)
    assert await restarted.run_once() == 0

    # The worker then executes the durable run to completion.
    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=queued)
    assert await worker.process_one() is True
    runs = await service.list_firings(tenant_id="tnt_1", trigger_id=trigger.id)
    assert len(runs) == 1
    final = await platform.workflow_runs.get_run(runs[0].workflow_run_id, tenant_id="tnt_1")
    assert final.status is WorkflowRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Recovery: a crashed mid-step workflow run is re-driven, not stalled
# ---------------------------------------------------------------------------
async def test_recovery_resets_inflight_steps_and_completes(make_platform, settings):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    queue = InMemoryJobQueue()
    platform.queue = queue
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(), name="recover", publish=True, steps=[_step("a")]
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED

    # Simulate a worker that died after marking the step RUNNING but before it
    # finished: the run is left non-terminal with an expired lease and the step
    # is stranded in RUNNING.
    steps = await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    stranded = steps[0].model_copy(update={"status": "running"})
    await platform.workflow_runs.update_step(stranded, expected_version=steps[0].version)
    stuck = run.model_copy(
        update={"status": WorkflowRunStatus.RUNNING, "lease_expires_at": None, "finished_at": None}
    )
    await platform.workflow_runs.update_run(stuck, expected_version=run.version)

    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=settings)
    assert await worker.recover_stale() == 1
    # The recovery sweep reset the stranded step so it can be re-driven.
    reset = await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    assert reset[0].status.value == "pending"

    assert await worker.process_one() is True
    final = await platform.workflow_runs.get_run(run.id, tenant_id="tnt_1")
    assert final.status is WorkflowRunStatus.COMPLETED
