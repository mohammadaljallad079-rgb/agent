"""End-to-end agent loop tests.

These exercise the *real* runner, executor, policy engine, task engine, context
manager and tools. Only the LLM is a deterministic fake, supplied through the
provider registry.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arabic_agent.core.enums import ApprovalStatus, RunStatus
from arabic_agent.models.base import ModelRequest, ModelResponse
from arabic_agent.permissions.base import Permission, Principal
from arabic_agent.tools.base import Tool, ToolContext, ToolResult

from .helpers import (
    is_planner,
    is_verifier,
    plan_json,
    scripted_responder,
    text_response,
    tool_response,
    verify_json,
)


def _principal(**kwargs: Any) -> Principal:
    defaults: dict[str, Any] = {"tenant_id": "tnt_1", "user_id": "usr_1"}
    defaults.update(kwargs)
    return Principal(**defaults)


async def test_agent_completes_simple_goal(make_platform, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("echo a greeting", "echo")),
        tool_calls=[("echo", {"text": "مرحبا"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم تنفيذ المهمة"),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="قل مرحبا", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.COMPLETED
    assert result.verified
    assert result.tool_calls >= 1
    assert "تم" in result.result


async def test_step_advertises_only_planned_and_core_tools(make_platform, tmp_path):
    """A step with a tool hint advertises that tool plus the core set only.

    This is what keeps a small local model from picking an unrelated tool (for
    example ``run_command``) when the plan already named the right one.
    """
    seen: list[ModelRequest] = []
    base = scripted_responder(
        plan=plan_json(("echo a greeting", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )

    def capturing(request: ModelRequest, n: int) -> ModelResponse:
        seen.append(request)
        return base(request, n)

    platform = make_platform(capturing)
    result = await platform.build_runner().run(
        principal=_principal(), goal="greet", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.COMPLETED

    advertised: set[str] = set()
    for request in seen:
        if is_planner(request) or is_verifier(request) or not request.tools:
            continue
        advertised |= {spec.name for spec in request.tools}
    assert "echo" in advertised
    assert "delete_file" not in advertised  # non-core tool is scoped out
    assert "write_file" in advertised  # core tool stays available


async def test_agent_pauses_for_approval_on_sensitive_tool(make_platform, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("write a file", "write_file")),
        tool_calls=[("write_file", {"path": "out.txt", "content": "hi"})],
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="اكتب ملف", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.WAITING_APPROVAL
    assert result.pending_approval_id is not None
    # The file must NOT exist yet: policy blocked it pending approval.
    assert not (tmp_path / "ws" / "out.txt").exists()
    pending = await platform.approvals.list_pending(tenant_id="tnt_1")
    assert len(pending) == 1
    assert pending[0].tool_name == "write_file"


async def test_approval_then_resume_completes_and_writes_file(make_platform, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("write a file", "write_file")),
        tool_calls=[
            ("write_file", {"path": "out.txt", "content": "مرحبا"}),
            ("finish", {"result": "تم"}),
        ],
        verification=verify_json(True, "تم"),
    )
    platform = make_platform(responder)
    runner = platform.build_runner()
    workspace = str(tmp_path / "ws")
    paused = await runner.run(principal=_principal(), goal="اكتب ملف", workspace_dir=workspace)
    assert paused.status is RunStatus.WAITING_APPROVAL

    approval = await platform.approvals.resolve(
        paused.pending_approval_id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    resumed = await runner.resume_after_approval(
        principal=_principal(),
        approval=approval,
        run_id=paused.run_id,
        workspace_dir=workspace,
    )
    assert resumed.status is RunStatus.COMPLETED
    assert (tmp_path / "ws" / "out.txt").read_text(encoding="utf-8") == "مرحبا"


class _DeleteTool(Tool):
    """A tool whose permission is denied by the default policy.

    Named distinctly from the real ``delete_file`` tool so this test asserts the
    *deny* path (not tool-name registration).
    """

    name = "purge_file"
    description = "delete a file (denied by default policy)"
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }
    permissions = (Permission.DELETE_FILE,)

    async def run(self, arguments: dict, context: ToolContext) -> ToolResult:
        raise AssertionError("a denied tool must never execute")


async def test_denied_tool_never_executes(make_platform, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("delete a file", "purge_file")),
        tool_calls=[("purge_file", {"path": "x"})],
    )
    platform = make_platform(responder, extra_tools=[_DeleteTool()])
    result = await platform.build_runner().run(
        principal=_principal(), goal="احذف ملف", workspace_dir=str(tmp_path / "ws")
    )
    # The agent cannot satisfy the goal; the run terminates as failed, no hang.
    assert result.status is RunStatus.FAILED


async def test_run_terminates_when_model_never_acts(make_platform, tmp_path):
    def respond(_request: ModelRequest, _n: int) -> ModelResponse:
        return text_response("أنا فقط أتحدث ولا أنفذ")

    platform = make_platform(respond)
    result = await platform.build_runner().run(
        principal=_principal(), goal="هدف", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.FAILED


async def test_not_found_error_triggers_replan_then_succeeds(make_platform, tmp_path):
    state = {"tool_turn": 0}

    def respond(request: ModelRequest, _n: int) -> ModelResponse:
        if is_verifier(request):
            return text_response(verify_json(True, "تم"))
        if is_planner(request):
            return text_response(plan_json(("read a file", "read_file")))
        turn = state["tool_turn"]
        state["tool_turn"] = turn + 1
        if turn == 0:
            # Missing file → NotFoundError → NOT_FOUND → replan.
            return tool_response("read_file", {"path": "missing.txt"}, call_id="c0")
        return tool_response("finish", {"result": "تم"}, call_id="c1")

    platform = make_platform(respond)
    result = await platform.build_runner().run(
        principal=_principal(), goal="اقرأ ملف", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.COMPLETED


async def test_replan_is_bounded_by_max_replans(make_platform, tmp_path):
    """A permanently failing step must not replan forever.

    Before this was enforced, the recovery policy was always consulted with
    ``replans_used=0``, so ``REPLAN`` was only ever stopped by the global step
    cap. Now the replan count is threaded through, so the run aborts as soon as
    ``max_replans`` is exhausted.
    """
    platform = make_platform(respond=_always_missing_file)
    result = await platform.build_runner().run(
        principal=_principal(), goal="اقرأ ملفا مفقودا", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.FAILED
    # The run stopped exactly at the configured replan bound, not at the step cap.
    assert result.replans == platform.settings.max_replans
    assert result.steps_executed < platform.settings.max_steps_per_run


def _always_missing_file(request: ModelRequest, _n: int) -> ModelResponse:
    if is_verifier(request):
        return text_response(verify_json(False, "لم تكتمل"))
    if is_planner(request):
        return text_response(plan_json(("read a file", "read_file")))
    return tool_response("read_file", {"path": "missing.txt"})


async def test_health_reports_service_and_version(make_platform, tmp_path):
    from fastapi.testclient import TestClient

    from arabic_agent import __version__
    from arabic_agent.api.app import create_app

    platform = make_platform(scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[]))
    with TestClient(create_app(platform, settings=platform.settings)) as client:
        body = client.get("/health").json()
    assert body["service"] == platform.settings.otel_service_name
    assert body["version"] == __version__


async def test_run_request_rejects_unknown_fields(make_platform):
    """Removed placeholder fields (resume/locale) must be rejected, not ignored."""
    from fastapi.testclient import TestClient

    from arabic_agent.api.app import create_app

    platform = make_platform(scripted_responder(plan=plan_json(("echo", "echo")), tool_calls=[]))
    with TestClient(create_app(platform, settings=platform.settings)) as client:
        response = client.post("/runs", json={"goal": "قل مرحبا", "resume": True})
    assert response.status_code == 422


async def test_audit_log_records_the_run(make_platform, tmp_path):
    responder = scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "hi"}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="say hi", workspace_dir=str(tmp_path / "ws")
    )
    history = await platform.audit.history(tenant_id="tnt_1", run_id=result.run_id)
    event_types = {e.type.value for e in history}
    assert "run.started" in event_types
    assert "plan.created" in event_types
    assert "tool.called" in event_types
    assert "run.completed" in event_types


async def test_secrets_never_appear_in_audit_log(make_platform, tmp_path):
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"
    responder = scripted_responder(
        plan=plan_json(("echo a secret", "echo")),
        tool_calls=[("echo", {"text": secret}), ("finish", {"result": "ok"})],
        verification=verify_json(True, "ok"),
    )
    platform = make_platform(responder)
    result = await platform.build_runner().run(
        principal=_principal(), goal="echo", workspace_dir=str(tmp_path / "ws")
    )
    history = await platform.audit.history(tenant_id="tnt_1", run_id=result.run_id)
    rendered = " ".join(str(e.data) + e.message for e in history)
    assert secret not in rendered
