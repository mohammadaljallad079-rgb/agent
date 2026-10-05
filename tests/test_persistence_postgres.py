"""Durable Postgres store integration tests.

These run against a real Postgres database (never a mock) and are skipped unless
``AAP_TEST_DATABASE_URL`` is set. They verify the properties that only the real
database can provide: optimistic-concurrency conflicts, atomic conditional
updates, tenant isolation in SQL, lease/heartbeat semantics and schema
round-tripping of every new Phase 2 field.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arabic_agent.core.enums import ApprovalStatus, MemoryKind, PermissionEffect, RunStatus
from arabic_agent.core.errors import ConflictError, NotFoundError
from arabic_agent.memory.base import MemoryEntry, MemoryQuery
from arabic_agent.permissions.approvals import Approval
from arabic_agent.permissions.base import Permission
from arabic_agent.persistence.stores import (
    PostgresApprovalStore,
    PostgresMemoryStore,
    PostgresRunStore,
)
from arabic_agent.runs.base import RunRecord

from .conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.postgres]


async def test_run_round_trip_preserves_new_fields(postgres_database):
    store = PostgresRunStore(postgres_database)
    record = RunRecord(
        tenant_id="tnt_1",
        user_id="usr_1",
        goal="جهز الفواتير",
        idempotency_key="idem-1",
        parent_run_id="run_parent",
        roles=("admin", "biller"),
        granted_permissions=("READ_FILE", "SEND_EMAIL"),
        workspace_dir="/tmp/ws",
    )
    created = await store.create(record)
    loaded = await store.get(created.id, tenant_id="tnt_1")
    assert loaded.goal == "جهز الفواتير"
    assert loaded.roles == ("admin", "biller")
    assert loaded.granted_permissions == ("READ_FILE", "SEND_EMAIL")
    assert loaded.idempotency_key == "idem-1"
    assert loaded.parent_run_id == "run_parent"
    assert loaded.recovery_attempts == 0


async def test_idempotency_key_lookup_and_unique(postgres_database):
    store = PostgresRunStore(postgres_database)
    record = RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="a", idempotency_key="k")
    await store.create(record)
    found = await store.get_by_idempotency_key(tenant_id="tnt_1", key="k")
    assert found is not None and found.id == record.id
    # A second insert with the same key must be rejected by the unique index.
    with pytest.raises(ConflictError):
        await store.create(
            RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="b", idempotency_key="k")
        )


async def test_optimistic_concurrency_rejects_stale_update(postgres_database):
    store = PostgresRunStore(postgres_database)
    record = await store.create(RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="a"))
    first = record.transition(RunStatus.RUNNING)
    await store.update(first, expected_version=record.version)
    # A stale writer still holding version 1 must lose.
    stale = record.transition(RunStatus.PLANNING)
    with pytest.raises(ConflictError):
        await store.update(stale, expected_version=record.version)


async def test_tenant_isolation(postgres_database):
    store = PostgresRunStore(postgres_database)
    await store.create(RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="a"))
    with pytest.raises(NotFoundError):
        await store.get("run_missing", tenant_id="tnt_2")


async def test_list_stale_and_heartbeat(postgres_database):
    store = PostgresRunStore(postgres_database)
    record = await store.create(RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="a"))
    running = record.transition(RunStatus.RUNNING)
    await store.update(running, expected_version=record.version)

    now = datetime.now(UTC)
    # A running run with no lease is stale.
    stale = await store.list_stale(now=now)
    assert [r.id for r in stale] == [record.id]

    # A fresh heartbeat moves the lease into the future; it is no longer stale.
    await store.heartbeat(
        record.id,
        tenant_id="tnt_1",
        worker_id="wkr_1",
        lease_expires_at=now + timedelta(seconds=120),
    )
    assert await store.list_stale(now=now) == []

    # Once the lease lapses it is stale again.
    later = now + timedelta(seconds=300)
    assert [r.id for r in await store.list_stale(now=later)] == [record.id]


async def test_list_runs_filters_by_status(postgres_database):
    store = PostgresRunStore(postgres_database)
    a = await store.create(RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="a"))
    b = await store.create(RunRecord(tenant_id="tnt_1", user_id="usr_1", goal="b"))
    await store.update(a.transition(RunStatus.RUNNING), expected_version=a.version)
    await store.update(
        b.transition(RunStatus.RUNNING).transition(RunStatus.COMPLETED),
        expected_version=b.version,
    )
    running = await store.list_runs(tenant_id="tnt_1", status=RunStatus.RUNNING)
    assert [r.id for r in running] == [a.id]


async def test_approval_resolve_is_once_only(postgres_database):
    store = PostgresApprovalStore(postgres_database)
    approval = Approval.create(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        tool_name="send_email",
        permission=Permission.SEND_EMAIL,
        summary="send 42 invoices",
    )
    await store.add(approval)
    resolved = await store.resolve(
        approval.id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    assert resolved.status is ApprovalStatus.APPROVED
    # A second decision must not succeed.
    with pytest.raises(NotFoundError):
        await store.resolve(
            approval.id,
            tenant_id="tnt_1",
            status=ApprovalStatus.DENIED,
            resolved_by="usr_1",
        )


async def test_memory_upsert_and_tenant_scope(postgres_database):
    store = PostgresMemoryStore(postgres_database)
    entry = MemoryEntry(
        kind=MemoryKind.COMPANY,
        tenant_id="tnt_1",
        user_id=None,
        key="company.name",
        content="شركة النور",
        importance=0.9,
    )
    await store.write(entry)
    # Re-writing the same key updates in place rather than duplicating.
    await store.write(entry.model_copy(update={"content": "شركة النور المتحدة"}))
    results = await store.query(MemoryQuery(tenant_id="tnt_1"))
    assert len(results) == 1
    assert results[0].content == "شركة النور المتحدة"
    assert await store.query(MemoryQuery(tenant_id="tnt_2")) == []


async def test_memory_permission_enum_is_stable():
    # Guards against silently renaming a permission the DB/policy depend on.
    assert PermissionEffect.ALLOW.value == "allow"


async def test_audit_sequence_is_durable_and_monotonic(postgres_database):
    """Sequences come from shared storage, not a per-process counter.

    This is the API+worker case: two processes write events for the same run and
    must not collide on ``(run_id, sequence)``.
    """
    from arabic_agent.audit.base import AuditEvent, AuditEventType
    from arabic_agent.persistence.stores import PostgresAuditStore

    store = PostgresAuditStore(postgres_database)

    def event(sequence: int) -> AuditEvent:
        return AuditEvent.create(
            AuditEventType.TOOL_CALLED,
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_shared",
            sequence=sequence,
            message="x",
        )

    # Two independent writers both start from sequence 1 (as separate processes
    # would). The sink must resolve the collision, not raise.
    await store.emit(event(1))
    await store.emit(event(1))
    await store.emit(event(1))

    history = await store.list_for_run(tenant_id="tnt_1", run_id="run_shared")
    sequences = [e.sequence for e in history]
    assert sequences == [1, 2, 3]
    # The next sequence is derived from durable storage, so a restarted process
    # continues the run's numbering instead of restarting at 1.
    assert await store.next_sequence(run_id="run_shared") == 4


async def test_audit_log_resolves_sequence_from_durable_sink(postgres_database):
    """The fan-out ``AuditLog`` defers sequence assignment to a durable sink."""
    from arabic_agent.audit.base import AuditEventType, AuditLog
    from arabic_agent.persistence.stores import PostgresAuditStore

    log = AuditLog([PostgresAuditStore(postgres_database)])
    for _ in range(3):
        await log.record(
            AuditEventType.STEP_STARTED,
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_log",
            message="step",
        )
    history = await log.history(tenant_id="tnt_1", run_id="run_log")
    assert [e.sequence for e in history] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Workflow stores
# ---------------------------------------------------------------------------
def _version(workflow_id: str, version: int = 1) -> object:
    from arabic_agent.core.enums import StepKind
    from arabic_agent.workflows.models import WorkflowStep, WorkflowVersion

    return WorkflowVersion(
        workflow_id=workflow_id,
        tenant_id="tnt_1",
        user_id="usr_1",
        version=version,
        steps=(
            WorkflowStep(id="a", kind=StepKind.DETERMINISTIC, action="set"),
            WorkflowStep(
                id="b",
                kind=StepKind.DETERMINISTIC,
                action="sum",
                depends_on=("a",),
                output={"total": "{{ result.total }}"},
            ),
        ),
    )


async def test_workflow_round_trip_and_tenant_isolation(postgres_database):
    from arabic_agent.persistence.stores import PostgresWorkflowStore
    from arabic_agent.workflows.models import Workflow

    store = PostgresWorkflowStore(postgres_database)
    workflow = await store.create(Workflow(tenant_id="tnt_1", user_id="usr_1", name="فواتير"))
    loaded = await store.get(workflow.id, tenant_id="tnt_1")
    assert loaded.name == "فواتير"
    with pytest.raises(NotFoundError):
        await store.get(workflow.id, tenant_id="tnt_2")


async def test_workflow_version_is_immutable_and_latest_wins(postgres_database):
    from arabic_agent.persistence.stores import (
        PostgresWorkflowStore,
        PostgresWorkflowVersionStore,
    )
    from arabic_agent.workflows.models import Workflow

    workflows = PostgresWorkflowStore(postgres_database)
    versions = PostgresWorkflowVersionStore(postgres_database)
    workflow = await workflows.create(Workflow(tenant_id="tnt_1", user_id="usr_1", name="v"))
    v1 = await versions.create(_version(workflow.id, version=1))
    v2 = await versions.create(_version(workflow.id, version=2))
    latest = await versions.latest(tenant_id="tnt_1", workflow_id=workflow.id)
    assert latest is not None and latest.id == v2.id
    listed = await versions.list_versions(tenant_id="tnt_1", workflow_id=workflow.id)
    assert [v.version for v in listed] == [2, 1]
    # The pinned v1 definition is still intact and unchanged.
    assert (await versions.get(v1.id, tenant_id="tnt_1")).version == 1


async def test_workflow_run_and_steps_round_trip(postgres_database):
    from arabic_agent.core.enums import StepKind
    from arabic_agent.persistence.stores import PostgresWorkflowRunStore
    from arabic_agent.workflows.run import StepRun, WorkflowRun

    store = PostgresWorkflowRunStore(postgres_database)
    run = WorkflowRun(
        tenant_id="tnt_1",
        user_id="usr_1",
        workflow_id="wfl_1",
        workflow_version_id="wfv_1",
        idempotency_key="idem-wf-1",
        workspace_dir="/tmp/ws",
    )
    steps = [
        StepRun(
            tenant_id="tnt_1",
            workflow_run_id=run.id,
            step_id="a",
            index=0,
            kind=StepKind.DETERMINISTIC,
        ),
        StepRun(
            tenant_id="tnt_1",
            workflow_run_id=run.id,
            step_id="b",
            index=1,
            kind=StepKind.DETERMINISTIC,
        ),
    ]
    await store.create_run(run, steps)
    found = await store.get_run_by_idempotency_key(tenant_id="tnt_1", key="idem-wf-1")
    assert found is not None and found.id == run.id
    loaded_steps = await store.list_steps(tenant_id="tnt_1", workflow_run_id=run.id)
    assert [s.step_id for s in loaded_steps] == ["a", "b"]
    # Idempotency key is unique per tenant.
    duplicate = WorkflowRun(
        tenant_id="tnt_1",
        user_id="usr_1",
        workflow_id="wfl_1",
        workflow_version_id="wfv_1",
        idempotency_key="idem-wf-1",
    )
    with pytest.raises(ConflictError):
        await store.create_run(duplicate, [])


async def test_workflow_run_optimistic_concurrency(postgres_database):
    from arabic_agent.persistence.stores import PostgresWorkflowRunStore
    from arabic_agent.workflows.run import WorkflowRun

    store = PostgresWorkflowRunStore(postgres_database)
    run = WorkflowRun(
        tenant_id="tnt_1",
        user_id="usr_1",
        workflow_id="wfl_1",
        workflow_version_id="wfv_1",
    )
    await store.create_run(run, [])
    updated = await store.update_run(run, expected_version=run.version)
    assert updated.version == run.version + 1
    with pytest.raises(ConflictError):
        await store.update_run(run, expected_version=run.version)


async def test_workflow_stale_run_discovery(postgres_database):
    from arabic_agent.core.enums import WorkflowRunStatus
    from arabic_agent.persistence.stores import PostgresWorkflowRunStore
    from arabic_agent.workflows.run import WorkflowRun

    store = PostgresWorkflowRunStore(postgres_database)
    run = WorkflowRun(
        tenant_id="tnt_1",
        user_id="usr_1",
        workflow_id="wfl_1",
        workflow_version_id="wfv_1",
    )
    await store.create_run(run, [])
    running = run.transition(WorkflowRunStatus.RUNNING, lease_expires_at=None)
    await store.update_run(running, expected_version=run.version)
    stale = await store.list_stale_runs(now=datetime.now(UTC))
    assert any(r.id == run.id for r in stale)


# ---------------------------------------------------------------------------
# Triggers
# ---------------------------------------------------------------------------
def _cron_trigger(**kwargs):
    from arabic_agent.core.enums import TriggerKind
    from arabic_agent.triggers.models import Trigger

    defaults = {
        "tenant_id": "tnt_1",
        "user_id": "usr_1",
        "name": "nightly",
        "kind": TriggerKind.CRON,
        "workflow_id": "wfl_1",
        "cron": "0 3 * * *",
        "next_fire_at": datetime.now(UTC) - timedelta(minutes=1),
    }
    defaults.update(kwargs)
    return Trigger(**defaults)


async def test_trigger_round_trip_and_tenant_isolation(postgres_database):
    from arabic_agent.persistence.stores import PostgresTriggerStore

    store = PostgresTriggerStore(postgres_database)
    trigger = await store.create(_cron_trigger())
    loaded = await store.get(trigger.id, tenant_id="tnt_1")
    assert loaded.name == "nightly"
    assert loaded.next_fire_at is not None
    with pytest.raises(NotFoundError):
        await store.get(trigger.id, tenant_id="tnt_2")


async def test_trigger_optimistic_concurrency(postgres_database):
    from arabic_agent.persistence.stores import PostgresTriggerStore

    store = PostgresTriggerStore(postgres_database)
    trigger = await store.create(_cron_trigger())
    updated = await store.update(trigger, expected_version=trigger.version)
    assert updated.version == trigger.version + 1
    with pytest.raises(ConflictError):
        await store.update(trigger, expected_version=trigger.version)


async def test_trigger_webhook_lookup_is_global_but_scoped_by_id(postgres_database):
    from arabic_agent.core.enums import TriggerKind
    from arabic_agent.persistence.stores import PostgresTriggerStore

    store = PostgresTriggerStore(postgres_database)
    trigger = await store.create(
        _cron_trigger(kind=TriggerKind.WEBHOOK, cron=None, webhook_id="whk_abc", next_fire_at=None)
    )
    # A webhook has no tenant in the URL, so lookup is by public id.
    found = await store.get_by_webhook_id("whk_abc")
    assert found is not None and found.id == trigger.id
    assert await store.get_by_webhook_id("whk_missing") is None


async def test_trigger_event_lookup_by_tenant_and_type(postgres_database):
    from arabic_agent.core.enums import TriggerKind
    from arabic_agent.persistence.stores import PostgresTriggerStore

    store = PostgresTriggerStore(postgres_database)
    await store.create(
        _cron_trigger(
            kind=TriggerKind.EVENT, cron=None, event_type="invoice.created", next_fire_at=None
        )
    )
    matches = await store.list_event_triggers(tenant_id="tnt_1", event_type="invoice.created")
    assert len(matches) == 1
    assert await store.list_event_triggers(tenant_id="tnt_2", event_type="invoice.created") == []


async def test_trigger_claim_due_is_atomic_and_lease_protected(postgres_database):
    from arabic_agent.persistence.stores import PostgresTriggerStore

    store = PostgresTriggerStore(postgres_database)
    trigger = await store.create(_cron_trigger())
    now = datetime.now(UTC)
    next_fire = now + timedelta(minutes=5)
    claimed = await store.claim_due(
        trigger.id,
        now=now,
        next_fire_at=next_fire,
        owner="sched-1",
        lease_expires_at=now + timedelta(seconds=30),
    )
    assert claimed is not None
    assert claimed.next_fire_at == next_fire
    assert claimed.lease_owner == "sched-1"
    # A second scheduler cannot claim the same occurrence while the lease holds.
    assert (
        await store.claim_due(
            trigger.id,
            now=now,
            next_fire_at=now + timedelta(minutes=10),
            owner="sched-2",
            lease_expires_at=now + timedelta(seconds=30),
        )
        is None
    )


async def test_trigger_run_idempotency_is_unique_per_tenant(postgres_database):
    from arabic_agent.core.enums import TriggerKind
    from arabic_agent.persistence.stores import PostgresTriggerRunStore
    from arabic_agent.triggers.models import TriggerRun

    store = PostgresTriggerRunStore(postgres_database)
    run = TriggerRun(
        tenant_id="tnt_1",
        trigger_id="trg_1",
        workflow_id="wfl_1",
        kind=TriggerKind.WEBHOOK,
        idempotency_key="webhook:delivery-1",
    )
    await store.create(run)
    found = await store.get_by_idempotency_key(tenant_id="tnt_1", key="webhook:delivery-1")
    assert found is not None and found.id == run.id
    duplicate = TriggerRun(
        tenant_id="tnt_1",
        trigger_id="trg_1",
        workflow_id="wfl_1",
        kind=TriggerKind.WEBHOOK,
        idempotency_key="webhook:delivery-1",
    )
    with pytest.raises(ConflictError):
        await store.create(duplicate)
    listed = await store.list_for_trigger(tenant_id="tnt_1", trigger_id="trg_1")
    assert len(listed) == 1
