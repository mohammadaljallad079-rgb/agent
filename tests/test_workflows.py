"""Workflow engine tests.

These drive the real engine, real stores, real policy engine, real approval
store and real tools. Only the LLM (when an agent step is used) is the
deterministic fake. Nothing about the engine is mocked: the point of the suite
is to prove that a workflow executes durably, in dependency order, with
branching, retries, approvals, recovery and tenant isolation all behaving.
"""

from __future__ import annotations

from typing import Any

import pytest

from arabic_agent.api.workflow_service import WorkflowService
from arabic_agent.core.enums import (
    ErrorClass,
    StepErrorPolicy,
    StepKind,
    StepRunStatus,
    WorkflowRunStatus,
)
from arabic_agent.core.errors import ConflictError, NotFoundError, ValidationError
from arabic_agent.permissions.base import Permission, Principal
from arabic_agent.worker.queue import InMemoryJobQueue, Job, JobType
from arabic_agent.worker.runtime import Worker
from arabic_agent.workflows.actions import DeterministicAction
from arabic_agent.workflows.models import RetryPolicy, WorkflowStep

from .helpers import plan_json, scripted_responder, verify_json


def _principal(**kwargs: Any) -> Principal:
    defaults: dict[str, Any] = {"tenant_id": "tnt_1", "user_id": "usr_1"}
    defaults.update(kwargs)
    return Principal(**defaults)


def _step(step_id: str, action: str, **kwargs: Any) -> WorkflowStep:
    return WorkflowStep(id=step_id, kind=StepKind.DETERMINISTIC, action=action, **kwargs)


# ---------------------------------------------------------------------------
# Graph validation
# ---------------------------------------------------------------------------
async def test_workflow_rejects_cycle(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    steps = [
        _step("a", "set", depends_on=("b",)),
        _step("b", "set", depends_on=("a",)),
    ]
    with pytest.raises(ValidationError, match="cycle"):
        await service.create(principal=_principal(), name="cyc", steps=steps)


async def test_workflow_rejects_unknown_dependency(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    steps = [_step("a", "set", depends_on=("ghost",))]
    with pytest.raises(ValidationError, match="unknown step"):
        await service.create(principal=_principal(), name="bad", steps=steps)


async def test_workflow_requires_kind_specific_fields(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    with pytest.raises(ValidationError):
        await service.create(
            principal=_principal(),
            name="badkind",
            steps=[WorkflowStep(id="a", kind=StepKind.TOOL)],
        )


# ---------------------------------------------------------------------------
# Execution: deterministic, ordering, output
# ---------------------------------------------------------------------------
async def test_sequential_workflow_with_output_templating(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="invoices",
        publish=True,
        steps=[
            _step("prepare", "set", arguments={"values": [1, 2]}),
            _step(
                "count",
                "sum",
                depends_on=("prepare",),
                arguments={"values": "{{ steps.prepare.output.values }}"},
            ),
            _step(
                "label",
                "concat",
                depends_on=("count",),
                arguments={
                    "parts": [
                        "count=",
                        "{{ steps.count.output.total }}",
                    ],
                    "separator": "",
                },
                output={"text": "{{ result.text }}"},
            ),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["count"].output["total"] == 3
    assert steps["label"].output["text"] == "count=3"
    assert run.output["label"]["text"] == "count=3"


async def test_parallel_fan_out_runs_independent_branches(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="fanout",
        publish=True,
        steps=[
            _step("root", "set", arguments={"n": 1}),
            _step("left", "sum", depends_on=("root",), arguments={"values": [1, 2]}),
            _step("right", "sum", depends_on=("root",), arguments={"values": [3, 4]}),
            _step(
                "join",
                "sum",
                depends_on=("left", "right"),
                arguments={
                    "values": [
                        "{{ steps.left.output.total }}",
                        "{{ steps.right.output.total }}",
                    ]
                },
            ),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["join"].output["total"] == 10


async def test_condition_skips_step(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="cond",
        publish=True,
        steps=[
            _step("gate", "set", arguments={"enabled": False}),
            _step(
                "maybe",
                "sum",
                depends_on=("gate",),
                condition="{{ steps.gate.output.enabled }}",
                arguments={"values": [1, 2, 3]},
            ),
            _step("after", "set", depends_on=("maybe",)),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["maybe"].status is StepRunStatus.SKIPPED
    # A skipped step still satisfies dependents.
    assert steps["after"].status is StepRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Retries and error policies
# ---------------------------------------------------------------------------
class _FlakyAction(DeterministicAction):
    name = "flaky"
    description = "Fails transiently a fixed number of times, then succeeds."

    def __init__(self, fail_times: int) -> None:
        self._remaining = fail_times
        super().__init__()

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._remaining > 0:
            self._remaining -= 1
            from arabic_agent.core.errors import TransientError

            raise TransientError("temporary failure")
        return {"ok": True}


class _FailingAction(DeterministicAction):
    name = "always_fails"
    description = "Always fails with a validation error (non-retryable)."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        from arabic_agent.core.errors import ValidationError

        raise ValidationError("permanent failure")


def _platform_with_actions(make_platform, actions: list[DeterministicAction]):
    """Build a platform whose workflow engine uses extra deterministic actions."""
    from arabic_agent.workflows.actions import default_actions

    registry = default_actions()
    for action in actions:
        registry.register(action)
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    return platform, registry


async def test_step_retries_transient_failure_then_succeeds(make_platform):
    platform, actions = _platform_with_actions(make_platform, [_FlakyAction(fail_times=2)])
    from arabic_agent.workflows.engine import WorkflowEngine

    platform.workflow_engine = WorkflowEngine(
        platform=platform,
        runs=platform.workflow_runs,
        versions=platform.workflow_versions,
        actions=actions,
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="retry",
        publish=True,
        steps=[
            _step(
                "flaky",
                "flaky",
                retry=RetryPolicy(max_attempts=3, backoff_seconds=0.0),
            )
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    step = (await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id))[0]
    assert step.status is StepRunStatus.COMPLETED
    assert step.attempts == 3


async def test_step_error_policy_skip_continues_run(make_platform):
    platform, actions = _platform_with_actions(make_platform, [_FailingAction()])
    from arabic_agent.workflows.engine import WorkflowEngine

    platform.workflow_engine = WorkflowEngine(
        platform=platform,
        runs=platform.workflow_runs,
        versions=platform.workflow_versions,
        actions=actions,
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="skip",
        publish=True,
        steps=[
            _step("boom", "always_fails", error_policy=StepErrorPolicy.SKIP),
            _step("after", "set", depends_on=("boom",)),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["boom"].status is StepRunStatus.SKIPPED
    assert steps["after"].status is StepRunStatus.COMPLETED


async def test_step_error_policy_fail_run_fails_whole_run(make_platform):
    platform, actions = _platform_with_actions(make_platform, [_FailingAction()])
    from arabic_agent.workflows.engine import WorkflowEngine

    platform.workflow_engine = WorkflowEngine(
        platform=platform,
        runs=platform.workflow_runs,
        versions=platform.workflow_versions,
        actions=actions,
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="fail",
        publish=True,
        steps=[_step("boom", "always_fails")],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.FAILED
    assert "boom" in (run.error or "")


async def test_step_error_policy_continue_keeps_run_going(make_platform):
    platform, actions = _platform_with_actions(make_platform, [_FailingAction()])
    from arabic_agent.workflows.engine import WorkflowEngine

    platform.workflow_engine = WorkflowEngine(
        platform=platform,
        runs=platform.workflow_runs,
        versions=platform.workflow_versions,
        actions=actions,
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="continue",
        publish=True,
        steps=[
            _step("boom", "always_fails", error_policy=StepErrorPolicy.CONTINUE),
            _step("after", "set", depends_on=("boom",)),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["boom"].status is StepRunStatus.FAILED
    assert steps["after"].status is StepRunStatus.COMPLETED


# ---------------------------------------------------------------------------
# Approvals (human in the loop)
# ---------------------------------------------------------------------------
async def test_approval_step_pauses_then_resumes(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="approve",
        publish=True,
        steps=[
            _step("prep", "set", arguments={"count": 42}),
            WorkflowStep(
                id="confirm",
                kind=StepKind.APPROVAL,
                depends_on=("prep",),
                prompt="تم إنشاء {{ steps.prep.output.count }} فاتورة. هل تريد الإرسال؟",
            ),
            _step("send", "set", depends_on=("confirm",)),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.WAITING_APPROVAL
    assert run.pending_approval_id is not None

    # The step after the approval must not have run yet.
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["confirm"].status is StepRunStatus.WAITING_APPROVAL
    assert steps["send"].status is StepRunStatus.PENDING

    resolved = await service.decide_approval(
        principal=_principal(), approval_id=run.pending_approval_id, approve=True
    )
    assert resolved.status is WorkflowRunStatus.COMPLETED
    steps = {
        s.step_id: s
        for s in await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id)
    }
    assert steps["confirm"].output["approved"] is True
    assert steps["send"].status is StepRunStatus.COMPLETED


async def test_denied_approval_fails_workflow_run(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="deny",
        publish=True,
        steps=[
            WorkflowStep(id="confirm", kind=StepKind.APPROVAL, prompt="موافق؟"),
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.WAITING_APPROVAL
    resolved = await service.decide_approval(
        principal=_principal(), approval_id=run.pending_approval_id, approve=False
    )
    assert resolved.status is WorkflowRunStatus.FAILED


async def test_tool_step_pauses_for_approval_then_writes(make_platform, tmp_path):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="toolstep",
        publish=True,
        steps=[
            WorkflowStep(
                id="write",
                kind=StepKind.TOOL,
                tool="write_file",
                arguments={"path": "out.txt", "content": "مرحبا"},
            )
        ],
    )
    workspace = str(tmp_path / "ws")
    run = await service.start_run(
        principal=_principal(), workflow_id=workflow.id, workspace_dir=workspace
    )
    assert run.status is WorkflowRunStatus.WAITING_APPROVAL
    # Policy blocked the write; nothing on disk yet.
    assert not (tmp_path / "ws" / "out.txt").exists()

    resolved = await service.decide_approval(
        principal=_principal(), approval_id=run.pending_approval_id, approve=True
    )
    assert resolved.status is WorkflowRunStatus.COMPLETED
    assert (tmp_path / "ws" / "out.txt").read_text() == "مرحبا"


# ---------------------------------------------------------------------------
# Agent steps
# ---------------------------------------------------------------------------
async def test_agent_step_runs_through_agent_runtime(make_platform):
    platform = make_platform(
        scripted_responder(
            plan=plan_json(("echo", "echo")),
            tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "تم"})],
            verification=verify_json(True, "تم"),
        )
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="agentstep",
        publish=True,
        steps=[
            WorkflowStep(
                id="think",
                kind=StepKind.AGENT,
                goal="قل مرحبا",
                output={"answer": "{{ result.result }}"},
            )
        ],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED
    step = (await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id))[0]
    assert step.agent_run_id is not None
    assert step.output["answer"] == "تم"


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------
class _SlowAction(DeterministicAction):
    name = "slow"
    description = "Sleeps longer than a test timeout."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        import asyncio

        await asyncio.sleep(5)
        return {}


async def test_step_timeout_fails_step(make_platform):
    platform, actions = _platform_with_actions(make_platform, [_SlowAction()])
    from arabic_agent.workflows.engine import WorkflowEngine

    platform.workflow_engine = WorkflowEngine(
        platform=platform,
        runs=platform.workflow_runs,
        versions=platform.workflow_versions,
        actions=actions,
    )
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="timeout",
        publish=True,
        steps=[_step("slow", "slow", timeout_seconds=0.05)],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.FAILED
    step = (await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id))[0]
    assert step.error_class is ErrorClass.TIMEOUT


# ---------------------------------------------------------------------------
# Idempotency, isolation, transition guards
# ---------------------------------------------------------------------------
async def test_start_run_is_idempotent(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(), name="idem", publish=True, steps=[_step("a", "set")]
    )
    first = await service.start_run(
        principal=_principal(), workflow_id=workflow.id, idempotency_key="k1"
    )
    second = await service.start_run(
        principal=_principal(), workflow_id=workflow.id, idempotency_key="k1"
    )
    assert first.id == second.id


async def test_workflow_is_tenant_scoped(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(tenant_id="tnt_1"),
        name="secret",
        publish=True,
        steps=[_step("a", "set")],
    )
    with pytest.raises(NotFoundError):
        await service.get(workflow.id, tenant_id="tnt_2")
    with pytest.raises(NotFoundError):
        await platform.workflow_runs.get_run(
            (
                await service.start_run(
                    principal=_principal(tenant_id="tnt_1"), workflow_id=workflow.id
                )
            ).id,
            tenant_id="tnt_2",
        )


async def test_terminal_step_cannot_restart(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(), name="guard", publish=True, steps=[_step("a", "set")]
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    step = (await service.list_step_runs(tenant_id="tnt_1", workflow_run_id=run.id))[0]
    assert step.status is StepRunStatus.COMPLETED
    from arabic_agent.core.enums import StepRunStatus as S

    with pytest.raises(ConflictError):
        step.transition(S.RUNNING)


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------
async def test_worker_recovers_stale_workflow_run(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    queue = InMemoryJobQueue()
    platform.queue = queue
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(), name="recover", publish=True, steps=[_step("a", "set")]
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.COMPLETED

    # Force the run back into a recoverable, expired-lease state to simulate a
    # worker that died mid-run.
    stuck = run.model_copy(
        update={"status": WorkflowRunStatus.RUNNING, "lease_expires_at": None, "finished_at": None}
    )
    await platform.workflow_runs.update_run(stuck, expected_version=run.version)

    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=platform.settings)
    recovered = await worker.recover_stale()
    assert recovered == 1
    assert await worker.process_one() is True
    final = await service.get_run(run.id, tenant_id="tnt_1")
    assert final.status is WorkflowRunStatus.COMPLETED


async def test_worker_ignores_waiting_approval_workflow(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    queue = InMemoryJobQueue()
    platform.queue = queue
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(),
        name="waiting",
        publish=True,
        steps=[WorkflowStep(id="confirm", kind=StepKind.APPROVAL, prompt="?")],
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.WAITING_APPROVAL
    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=platform.settings)
    assert await worker.recover_stale() == 0


async def test_queued_workflow_job_is_executed_by_worker(make_platform, settings):
    queued_settings = settings.model_copy(update={"execution_mode": "queued"})
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    # Rebuild with queued mode by swapping settings on the platform and service.
    platform.settings = queued_settings
    queue = InMemoryJobQueue()
    platform.queue = queue
    service = WorkflowService(platform, settings=queued_settings)
    workflow, _ = await service.create(
        principal=_principal(), name="queued", publish=True, steps=[_step("a", "set")]
    )
    run = await service.start_run(principal=_principal(), workflow_id=workflow.id)
    assert run.status is WorkflowRunStatus.PENDING

    worker = Worker(platform=platform, runs=platform.runs, queue=queue, settings=queued_settings)
    assert await worker.process_one() is True
    final = await service.get_run(run.id, tenant_id="tnt_1")
    assert final.status is WorkflowRunStatus.COMPLETED


async def test_permission_denied_without_run_workflow(make_platform):
    platform = make_platform(scripted_responder(plan=plan_json(("x", "echo")), tool_calls=[]))
    service = WorkflowService(platform)
    workflow, _ = await service.create(
        principal=_principal(), name="perm", publish=True, steps=[_step("a", "set")]
    )
    restricted = Principal(
        tenant_id="tnt_1",
        user_id="usr_2",
        granted_permissions=frozenset({Permission.READ_FILE}),
    )
    from arabic_agent.core.errors import PermissionDeniedError

    with pytest.raises(PermissionDeniedError):
        await service.start_run(principal=restricted, workflow_id=workflow.id)


async def test_job_type_enum_has_workflow_variants():
    assert JobType.START_WORKFLOW.value == "start_workflow"
    assert JobType.RESUME_WORKFLOW.value == "resume_workflow"
    assert JobType.RECOVER_WORKFLOW.value == "recover_workflow"
    job = Job(type=JobType.START_WORKFLOW, run_id="wr_1", tenant_id="t1", user_id="u1")
    assert job.type is JobType.START_WORKFLOW
