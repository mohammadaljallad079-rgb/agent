"""Durable local runtime: restart and recovery tests (real SQLite file).

Phase 4B acceptance tests. Unlike the unit suite, these use a **real SQLite file
on disk** and, for the cross-restart approval case, a **real second process**.
They prove the claims the in-memory local runtime could not make:

* state written by one platform is read back by the next (durable persistence);
* a workflow run left mid-flight by a lost worker is reclaimed and driven to
  completion by a fresh platform (real recovery on real durable state);
* a pending approval survives a process restart and can be resolved afterwards;
* the local store enforces the same tenant-isolation and optimistic-concurrency
  contract as Postgres;
* an untrusted payload is persisted and read back as data, never executed.

The model provider is the only deterministic double; every store, the workflow
engine and the recovery coordinator are the production implementations.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.api.workflow_service import WorkflowService
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import (
    ApprovalStatus,
    OperatingMode,
    RuntimeKind,
    StepKind,
    StepRunStatus,
    SyncKind,
    WorkflowRunStatus,
)
from arabic_agent.core.errors import ConflictError, NotFoundError
from arabic_agent.local.sync import SyncAction
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.permissions.base import Permission, Principal
from arabic_agent.worker.queue import InMemoryJobQueue
from arabic_agent.worker.runtime import Worker
from arabic_agent.workflows.models import WorkflowStep

from .helpers import FakeProvider, plan_json, scripted_responder, verify_json

REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _principal(**kwargs: object) -> Principal:
    defaults: dict[str, object] = {
        "tenant_id": "tnt_1",
        "user_id": "usr_1",
        "roles": frozenset({"admin"}),
        "granted_permissions": frozenset(
            {Permission.MANAGE_WORKFLOW, Permission.RUN_WORKFLOW, Permission.WRITE_FILE}
        ),
    }
    defaults.update(kwargs)
    return Principal(**defaults)  # type: ignore[arg-type]


def _settings(tmp_path: Path, *, execution_mode: str = "inline") -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "data"),
        local_workspace_roots=(str(tmp_path / "approved"),),
        local_mode=OperatingMode.OFFLINE,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode=execution_mode,
        max_steps_per_run=20,
    )


def _build(tmp_path: Path, *, execution_mode: str = "inline") -> Platform:
    """Build a real local platform backed by a real on-disk SQLite store."""
    registry = ProviderRegistry()
    registry.register(
        FakeProvider(
            scripted_responder(
                plan=plan_json(("echo", "echo")),
                tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم"})],
                verification=verify_json(True, "تم"),
            )
        )
    )
    return Platform.build(
        _settings(tmp_path, execution_mode=execution_mode),
        provider_registry=registry,
        default_model="fake/model",
    )


def _step(step_id: str, action: str, **kwargs: object) -> WorkflowStep:
    return WorkflowStep(
        id=step_id,
        kind=StepKind.DETERMINISTIC,
        action=action,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Durable persistence across a platform restart
# ---------------------------------------------------------------------------
async def test_local_platform_is_durable_and_migrated(tmp_path):
    platform = _build(tmp_path)
    try:
        assert platform.durable is True
        assert platform.local_database is not None
        assert platform.database is None  # no Postgres in local mode
        assert platform.local_database.path.is_file()
        # The schema is owned by Alembic and migrated, not created ad hoc.
        con = sqlite3.connect(platform.local_database.path)
        try:
            tables = {
                row[0] for row in con.execute("select name from sqlite_master where type='table'")
            }
            assert "local_alembic_version" in tables
            assert "local_workflow_runs" in tables
            assert list(con.execute("select version_num from local_alembic_version"))
        finally:
            con.close()
    finally:
        await platform.aclose()


async def test_migrations_resolve_for_installed_package(tmp_path):
    """The migration chain is found from an installed package layout.

    A packaged desktop install has no repository root, so the default migration
    directory must resolve inside the package (``_migrations_local``). This
    guards the install-time path that previously pointed at a repo-relative
    ``parents[4]`` and failed on a real install.
    """
    from arabic_agent.local.persistence import database as db_module

    resolved = db_module._default_migrations_dir()
    assert resolved.is_dir()
    assert (resolved / "env.py").is_file()
    assert (resolved / "versions").is_dir()
    assert any((resolved / "versions").glob("*.py"))

    # And an upgrade with no explicit dir uses it to create the schema.
    store = db_module.LocalDatabase(str(tmp_path / "local.db"))
    await store.upgrade_async()
    con = sqlite3.connect(store.path)
    try:
        rows = con.execute("select name from sqlite_master where type='table'")
        tables = {row[0] for row in rows}
    finally:
        con.close()
    assert {"local_alembic_version", "local_runs", "local_action_ledger"} <= tables
    await store.dispose()


async def test_workflow_run_survives_platform_restart(tmp_path):
    platform1 = _build(tmp_path)
    service1 = WorkflowService(platform1)
    principal = _principal()
    workflow, _ = await service1.create(
        principal=principal,
        name="durable-report",
        publish=True,
        steps=[
            _step("prepare", "set", arguments={"values": [1, 2]}),
            _step(
                "count",
                "sum",
                depends_on=("prepare",),
                arguments={"values": "{{ steps.prepare.output.values }}"},
                output={"total": "{{ result.total }}"},
            ),
        ],
    )
    run = await service1.start_run(principal=principal, workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    run_id = run.id
    await platform1.aclose()  # simulate the process going away

    # A brand-new platform on the same store sees the run and its step state.
    platform2 = _build(tmp_path)
    try:
        assert platform2.local_database is not None
        reloaded = await platform2.workflow_runs.get_run(run_id, tenant_id="tnt_1")
        assert reloaded.status is WorkflowRunStatus.COMPLETED
        assert reloaded.output["count"]["total"] == 3
        steps = await platform2.workflow_runs.list_steps(tenant_id="tnt_1", workflow_run_id=run_id)
        by_id = {s.step_id: s for s in steps}
        assert by_id["count"].status is StepRunStatus.COMPLETED
        assert by_id["count"].output["total"] == 3
    finally:
        await platform2.aclose()


async def test_workflow_run_recovers_after_worker_loss(tmp_path):
    """A run stranded mid-flight by a lost worker is reclaimed by a new platform."""
    platform1 = _build(tmp_path)
    service1 = WorkflowService(platform1)
    principal = _principal()
    workflow, _ = await service1.create(
        principal=principal, name="recoverable", publish=True, steps=[_step("a", "set")]
    )
    run = await service1.start_run(principal=principal, workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED

    # Simulate a worker that died mid-run: RUNNING with an expired lease.
    stranded = run.model_copy(
        update={
            "status": WorkflowRunStatus.RUNNING,
            "lease_expires_at": None,
            "finished_at": None,
        }
    )
    await platform1.workflow_runs.update_run(stranded, expected_version=run.version)
    await platform1.aclose()  # the "worker process" is gone

    # A fresh platform reclaims the stranded run and drives it to completion.
    platform2 = _build(tmp_path)
    try:
        queue = InMemoryJobQueue()
        platform2.queue = queue
        worker = Worker(
            platform=platform2, runs=platform2.runs, queue=queue, settings=platform2.settings
        )
        assert await worker.recover_stale() == 1
        assert await worker.process_one() is True
        final = await platform2.workflow_runs.get_run(run.id, tenant_id="tnt_1")
        assert final.status is WorkflowRunStatus.COMPLETED
        # The recovery is observable in the durable audit trail.
        events = await platform2.audit.list_for_run(tenant_id="tnt_1", run_id=run.id)
        assert any("recovery" in e.message for e in events)
    finally:
        await platform2.aclose()


async def test_sync_queue_survives_restart(tmp_path):
    platform1 = _build(tmp_path)
    action = SyncAction(
        tenant_id="tnt_1",
        user_id="usr_1",
        device_id="dev_1",
        kind=SyncKind.DEVICE_STATUS,
        payload={"state": "offline"},
    )
    await platform1.sync_queue.add(action)
    await platform1.aclose()

    platform2 = _build(tmp_path)
    try:
        pending = await platform2.sync_queue.list_pending(tenant_id="tnt_1")
        assert [a.id for a in pending] == [action.id]
        assert pending[0].payload["state"] == "offline"
    finally:
        await platform2.aclose()


# ---------------------------------------------------------------------------
# Tenant isolation and optimistic concurrency (same contract as Postgres)
# ---------------------------------------------------------------------------
async def test_local_store_enforces_tenant_isolation(tmp_path):
    platform = _build(tmp_path)
    try:
        service = WorkflowService(platform)
        workflow, _ = await service.create(
            principal=_principal(tenant_id="tnt_a"),
            name="isolated",
            publish=True,
            steps=[_step("a", "set")],
        )
        # Another tenant must not see it, even by exact id.
        with pytest.raises(NotFoundError):
            await platform.workflows.get(workflow.id, tenant_id="tnt_b")
        with pytest.raises(NotFoundError):
            await platform.workflow_versions.get(
                workflow.latest_version_id or "", tenant_id="tnt_b"
            )
        assert await platform.workflows.list_workflows(tenant_id="tnt_b") == []
    finally:
        await platform.aclose()


async def test_local_store_optimistic_concurrency(tmp_path):
    platform = _build(tmp_path)
    try:
        service = WorkflowService(platform)
        workflow, _ = await service.create(
            principal=_principal(), name="cc", publish=True, steps=[_step("a", "set")]
        )
        run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
        stale = await platform.workflow_runs.get_run(run.id, tenant_id="tnt_1")
        # A correct update advances the version...
        await platform.workflow_runs.update_run(stale, expected_version=stale.version)
        # ...so replaying the same expected_version must conflict, not overwrite.
        with pytest.raises(ConflictError):
            await platform.workflow_runs.update_run(stale, expected_version=stale.version)
    finally:
        await platform.aclose()


# ---------------------------------------------------------------------------
# Security: untrusted payloads are data, never executed
# ---------------------------------------------------------------------------
async def test_untrusted_payload_is_stored_as_data_not_executed(tmp_path):
    platform = _build(tmp_path)
    try:
        service = WorkflowService(platform)
        injection = "'; DROP TABLE local_workflows;--"
        workflow, _ = await service.create(
            principal=_principal(),
            name="inj",
            description=injection,
            publish=True,
            steps=[_step("a", "set", arguments={"values": [injection]})],
        )
        # Read it back verbatim: the payload round-trips as data.
        reloaded = await platform.workflows.get(workflow.id, tenant_id="tnt_1")
        assert reloaded.description == injection

        # The table still exists and still holds the row (the injection never ran).
        assert platform.local_database is not None
        con = sqlite3.connect(platform.local_database.path)
        try:
            assert (
                con.execute(
                    "select count(*) from local_workflows where id = ?", (workflow.id,)
                ).fetchone()[0]
                == 1
            )
        finally:
            con.close()
    finally:
        await platform.aclose()


# ---------------------------------------------------------------------------
# Cross-process: approval survives a real process restart, then resumes
# ---------------------------------------------------------------------------
_CHILD_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, sys
    from pydantic import SecretStr
    from arabic_agent.api.workflow_service import WorkflowService
    from arabic_agent.container import Platform
    from arabic_agent.core.config import Settings
    from arabic_agent.core.enums import OperatingMode, RuntimeKind, StepKind
    from arabic_agent.models.registry import ProviderRegistry
    from arabic_agent.permissions.base import Permission, Principal
    from arabic_agent.workflows.models import WorkflowStep
    from tests.helpers import FakeProvider, plan_json, scripted_responder, verify_json

    store_dir, approved = sys.argv[1], sys.argv[2]
    settings = Settings(
        environment="test", require_auth=False, runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=store_dir, local_workspace_roots=(approved,),
        local_mode=OperatingMode.OFFLINE, database_url=SecretStr(""), redis_url=SecretStr(""),
        execution_mode="inline",
    )
    registry = ProviderRegistry()
    registry.register(FakeProvider(scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )))
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")

    async def main():
        service = WorkflowService(platform)
        principal = Principal(
            tenant_id="tnt_1", user_id="usr_1", roles=frozenset({"admin"}),
            granted_permissions=frozenset(
                {Permission.MANAGE_WORKFLOW, Permission.RUN_WORKFLOW}
            ),
        )
        workflow, _ = await service.create(
            principal=principal, name="needs-approval", publish=True,
            steps=[WorkflowStep(id="confirm", kind=StepKind.APPROVAL, prompt="send?")],
        )
        run = await service.start_run(principal=principal, workflow_id=workflow.id)
        print(json.dumps({
            "run_id": run.id,
            "approval_id": run.pending_approval_id,
            "status": run.status.value,
        }))
        await platform.aclose()

    asyncio.run(main())
    """
)


async def test_approval_survives_process_restart_then_resumes(tmp_path):
    store = tmp_path / "data"
    approved = tmp_path / "approved"
    env = {**os.environ, "PYTHONPATH": REPO_ROOT}
    # A single, bounded subprocess launch; blocking here is deliberate.
    proc = subprocess.run(  # noqa: S603,ASYNC221 - fixed interpreter and literal script
        [sys.executable, "-c", _CHILD_SCRIPT, str(store), str(approved)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["status"] == WorkflowRunStatus.WAITING_APPROVAL.value
    assert payload["approval_id"]

    # This test process is a *new* process: it must see the paused run and its
    # pending approval from the durable store alone.
    platform = _build(tmp_path)
    try:
        run = await platform.workflow_runs.get_run(payload["run_id"], tenant_id="tnt_1")
        assert run.status is WorkflowRunStatus.WAITING_APPROVAL
        approval = await platform.approvals.get(payload["approval_id"], tenant_id="tnt_1")
        assert approval.status is ApprovalStatus.PENDING

        resolved = await platform.approvals.resolve(
            approval.id,
            tenant_id="tnt_1",
            status=ApprovalStatus.APPROVED,
            resolved_by="usr_1",
            reason="approved after restart",
        )
        assert resolved.status is ApprovalStatus.APPROVED

        resumed = await platform.workflow_engine.resume_run(
            run.id, tenant_id="tnt_1", approval_id=approval.id
        )
        assert resumed is not None
        assert resumed.status is WorkflowRunStatus.COMPLETED
    finally:
        await platform.aclose()


async def test_approval_preview_is_durable_and_tenant_scoped(tmp_path):
    """A structured approval preview survives a restart and stays tenant-scoped."""
    from datetime import UTC, datetime, timedelta

    from arabic_agent.permissions.approvals import Approval

    preview = {
        "operation": "write_file",
        "path": "report.md",
        "bytes": 12,
        "overwrites_existing": True,
    }
    platform = _build(tmp_path)
    try:
        saved = await platform.approvals.add(
            Approval(
                tenant_id="tnt_1",
                user_id="usr_1",
                run_id="run_preview",
                tool_name="write_file",
                permission=Permission.WRITE_FILE,
                arguments={"path": "report.md"},
                summary="اكتب الملف report.md",
                preview=preview,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
    finally:
        await platform.aclose()

    # A new platform (new process in the real world) reads it back from disk.
    reopened = _build(tmp_path)
    try:
        loaded = await reopened.approvals.get(saved.id, tenant_id="tnt_1")
        assert loaded.preview == preview
        # Cross-tenant access must not resolve the record.
        with pytest.raises(NotFoundError):
            await reopened.approvals.get(saved.id, tenant_id="tnt_2")
    finally:
        await reopened.aclose()


# ---------------------------------------------------------------------------
# Specialist selection reaches the executor and is enforced durably
# ---------------------------------------------------------------------------
async def test_specialist_selection_reaches_the_executor_and_is_durable(tmp_path):
    """A chosen specialist is persisted, executed under its scope, and re-read.

    This is the durable half of the desktop selector: the id the UI sends is
    stored on the run row, reaches the executor, and survives a restart.
    """
    from arabic_agent.api.service import RunService

    platform = _build(tmp_path)
    service = RunService(platform)
    try:
        record = await service.start(
            principal=_principal(),
            goal="حلّل المشروع",
            specialist="coding",
        )
        assert record.specialist == "coding"
        # The executor advertises the coding scope, which excludes a tool only
        # the general agent should have (e.g. printing).
        assert "run_local_command" in platform.specialists.get("coding").advertised_tools()
        assert not platform.specialists.get("coding").allows("print_document")
        # The general agent, by contrast, allows everything.
        assert platform.specialists.get("general").allows("print_document")
    finally:
        await platform.aclose()

    # A new platform reads the specialist back from the real SQLite file.
    reopened = _build(tmp_path)
    try:
        loaded = await reopened.runs.get(record.id, tenant_id="tnt_1")
        assert loaded.specialist == "coding"
    finally:
        await reopened.aclose()


async def test_run_defaults_to_general_specialist_when_omitted(tmp_path):
    """Backward compatibility: an existing caller that omits it gets general."""
    from arabic_agent.api.service import RunService

    platform = _build(tmp_path)
    service = RunService(platform)
    try:
        record = await service.start(principal=_principal(), goal="مهمة عامة")
        assert record.specialist == "general"
    finally:
        await platform.aclose()


# ---------------------------------------------------------------------------
# Security: secrets never persist, finished work is never resurrected
# ---------------------------------------------------------------------------
async def test_audit_redacts_secrets_before_persisting(tmp_path):
    platform = _build(tmp_path)
    try:
        await platform.audit.record(
            "run.started",  # type: ignore[arg-type]
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_secret",
            message="call the provider",
            data={"api_key": "sk-live-abcdefghijklmnopqrstuvwxyz", "note": "safe"},
        )
        events = await platform.audit.list_for_run(tenant_id="tnt_1", run_id="run_secret")
        assert events, "the event must be persisted for the trail to be useful"
        payload = events[0].data
        assert payload["api_key"] == "***REDACTED***"
        assert payload["note"] == "safe"
        # Belt and braces: the raw secret is nowhere in the stored row.
        assert platform.local_database is not None
        con = sqlite3.connect(platform.local_database.path)
        try:
            blob = " ".join(str(r[0]) for r in con.execute("select data from local_audit_events"))
        finally:
            con.close()
        assert "sk-live" not in blob
    finally:
        await platform.aclose()


async def test_completed_run_is_not_resurrected_after_restart(tmp_path):
    """A finished run with an expired lease must never be re-executed."""
    platform1 = _build(tmp_path)
    service1 = WorkflowService(platform1)
    workflow, _ = await service1.create(
        principal=_principal(), name="done", publish=True, steps=[_step("a", "set")]
    )
    run = await service1.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    # Terminal, but with a lease in the past: still must not be reclaimed.
    terminal = run.model_copy(update={"lease_expires_at": None})
    await platform1.workflow_runs.update_run(terminal, expected_version=run.version)
    await platform1.aclose()

    platform2 = _build(tmp_path)
    try:
        queue = InMemoryJobQueue()
        platform2.queue = queue
        worker = Worker(
            platform=platform2, runs=platform2.runs, queue=queue, settings=platform2.settings
        )
        assert await worker.recover_stale() == 0
        assert await worker.process_one() is False
        final = await platform2.workflow_runs.get_run(run.id, tenant_id="tnt_1")
        assert final.status is WorkflowRunStatus.COMPLETED
    finally:
        await platform2.aclose()
