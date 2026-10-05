"""Phase 6: loop protection.

The guard is a pure, deterministic counter; these tests pin its behaviour and
then prove the agent loop terminates a run that repeats the same action instead
of spinning to the coarse max-steps limit.
"""

from __future__ import annotations

import pytest

from arabic_agent.agent.loops import LoopGuard, call_signature


def test_signature_is_stable_and_key_order_independent() -> None:
    a = call_signature("read_file", {"path": "x", "start": 1})
    b = call_signature("read_file", {"start": 1, "path": "x"})
    assert a == b
    assert a != call_signature("read_file", {"path": "y"})


def test_repeat_soft_then_hard() -> None:
    guard = LoopGuard(repeat_soft_limit=2, repeat_hard_limit=4)
    verdicts = [guard.observe_call("read_file", {"path": "x"}).action for _ in range(4)]
    assert verdicts[0] == "allow"
    assert verdicts[1] == "allow"
    assert verdicts[2] == "warn"
    assert verdicts[3] == "abort"


def test_different_arguments_do_not_trip() -> None:
    guard = LoopGuard()
    for i in range(10):
        assert guard.observe_call("read_file", {"path": f"f{i}"}).action == "allow"


def test_repeated_failure_trips() -> None:
    guard = LoopGuard(failure_limit=3)
    actions = [guard.observe_failure("run_command", "external").action for _ in range(3)]
    assert actions[-1] == "abort"


# --- through the agent loop -------------------------------------------------


@pytest.mark.asyncio
async def test_agent_aborts_on_repeated_identical_tool_call(make_platform, tmp_path) -> None:
    from arabic_agent.core.enums import RunStatus
    from arabic_agent.models.base import ModelRequest, ModelResponse
    from tests.helpers import is_planner, is_verifier, plan_json, text_response, tool_response

    def respond(request: ModelRequest, _n: int) -> ModelResponse:
        if is_verifier(request):
            return text_response("{}")
        if is_planner(request):
            # Ten steps so the run would otherwise proceed past the guard.
            return text_response(
                plan_json(*[("repeat the same action", "echo") for _ in range(10)])
            )
        # Always call the *same* tool with the *same* arguments (it succeeds,
        # so recovery never masks the repetition).
        return tool_response("echo", {"text": "x"}, call_id="c1")

    platform = make_platform(respond)
    result = await platform.build_runner().run(
        principal=_principal(), goal="كرر نفس العملية", workspace_dir=str(tmp_path / "ws")
    )
    assert result.status is RunStatus.FAILED
    assert "identical arguments" in (result.error or "")


def _principal():
    from arabic_agent.permissions.base import Principal

    return Principal(tenant_id="t1", user_id="u1")
