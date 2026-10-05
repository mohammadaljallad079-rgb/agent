"""Computer tool tests: permission gating, approvals and audit (Phase 7).

The tools are ordinary tools, so these run the *real* executor, policy engine,
approval store and audit log against a scripted backend. They prove that
computer control is not a bypass: reading the screen, moving the pointer, typing
and focusing windows all pass through the same allow / deny / requires-approval
gate as any other tool, and every executed call is audited.
"""

from __future__ import annotations

import pytest

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.computer.base import WindowInfo
from arabic_agent.computer.grounding import GroundingTarget
from arabic_agent.computer.perception import PerceptionLayer
from arabic_agent.computer.policy import ComputerPolicy
from arabic_agent.computer.runtime import ComputerRuntime
from arabic_agent.computer.screen_boundary import ScreenDataBoundary, ScreenDataPolicy
from arabic_agent.core.enums import PermissionEffect
from arabic_agent.core.errors import ConfigurationError, ValidationError
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.computer import COMPUTER_TOOLS
from arabic_agent.tools.registry import ToolRegistry

from .fake_computer import FakeComputerBackend, FakeOcr


def _runtime(audit: AuditLog) -> ComputerRuntime:
    backend = FakeComputerBackend(
        windows=[WindowInfo(id="win_1", title="Openbox", active=True, width=100, height=100)]
    )
    return ComputerRuntime(
        backend=backend,
        perception=PerceptionLayer(backend, ocr=FakeOcr()),
        policy=ComputerPolicy(),
        audit=audit,
        boundary=ScreenDataBoundary(policy=ScreenDataPolicy.LOCAL_ONLY),
    )


def _allow_policy() -> PolicyEngine:
    """A policy that allows every computer permission (for execution tests)."""
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-computer",
            rules=tuple(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=perm,
                    reason="test allow",
                )
                for perm in (
                    Permission.SCREEN_READ,
                    Permission.POINTER_CONTROL,
                    Permission.KEYBOARD_CONTROL,
                    Permission.WINDOW_CONTROL,
                )
            ),
        ),
    )
    return engine


def _executor(*, policy: PolicyEngine | None = None):
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    runtime = _runtime(audit)
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in COMPUTER_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=audit,
        tool_metadata={"computer_runtime": runtime},
    )
    return executor, sink, runtime


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def _context() -> ToolContext:
    return ToolContext(tenant_id="tnt_1", user_id="usr_1", run_id="run_1")


async def test_screen_tool_executes_and_audits():
    executor, sink, _ = _executor()
    outcome = await executor.execute(
        tool_name="computer_screen", arguments={}, context=_context(), principal=_principal()
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert outcome.result is not None and outcome.result.ok
    assert "screenshot" in outcome.result.summary
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.COMPUTER_SCREEN_CAPTURED for e in events)


async def test_pointer_tool_requires_approval_under_default_policy():
    executor, _, _ = _executor(policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="computer_pointer",
        arguments={"action": "click", "x": 10, "y": 10},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.POINTER_CONTROL


async def test_pointer_tool_executes_when_allowed():
    executor, _, _ = _executor()
    # A screen read is required first so a fresh observation exists.
    await executor.execute(
        tool_name="computer_screen", arguments={}, context=_context(), principal=_principal()
    )
    outcome = await executor.execute(
        tool_name="computer_pointer",
        arguments={"action": "move", "x": 100, "y": 120},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert outcome.result is not None and outcome.result.ok
    assert "100" in outcome.result.summary


async def test_keyboard_tool_types_arabic_and_reports_length_only():
    executor, sink, _ = _executor()
    await executor.execute(
        tool_name="computer_screen", arguments={}, context=_context(), principal=_principal()
    )
    outcome = await executor.execute(
        tool_name="computer_keyboard",
        arguments={"action": "type", "text": "مرحبا بالعالم"},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    typed = [e for e in events if e.type is AuditEventType.COMPUTER_TEXT_TYPED]
    assert typed and typed[0].data.get("length") == len("مرحبا بالعالم")


async def test_window_tool_lists_and_focuses():
    executor, _, _ = _executor()
    listed = await executor.execute(
        tool_name="computer_window",
        arguments={"action": "list"},
        context=_context(),
        principal=_principal(),
    )
    assert listed.kind is ExecutionOutcomeKind.COMPLETED
    assert listed.result is not None and listed.result.data["windows"]

    focused = await executor.execute(
        tool_name="computer_window",
        arguments={"action": "focus", "window_id": "win_1"},
        context=_context(),
        principal=_principal(),
    )
    assert focused.kind is ExecutionOutcomeKind.COMPLETED
    assert focused.result is not None and focused.result.data["focused"] is True


async def test_denied_permission_blocks_computer_tool():
    policy = PolicyEngine()
    policy.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="deny-pointer",
            rules=(
                Rule(
                    effect=PermissionEffect.DENY,
                    permission=Permission.POINTER_CONTROL,
                    reason="no pointer control",
                ),
            ),
        ),
    )
    executor, _, _ = _executor(policy=policy)
    outcome = await executor.execute(
        tool_name="computer_pointer",
        arguments={"action": "click", "x": 1, "y": 1},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.DENIED


async def test_computer_tool_without_runtime_fails_honestly():
    sink = InMemoryAuditSink()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in COMPUTER_TOOLS]),
        policy=_allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=AuditLog([sink]),
    )
    with pytest.raises(ConfigurationError):
        await executor.execute(
            tool_name="computer_screen", arguments={}, context=_context(), principal=_principal()
        )


async def test_pointer_scroll_requires_amount():
    executor, _, _ = _executor()
    await executor.execute(
        tool_name="computer_screen", arguments={}, context=_context(), principal=_principal()
    )
    with pytest.raises(ValidationError):
        await executor.execute(
            tool_name="computer_pointer",
            arguments={"action": "scroll"},
            context=_context(),
            principal=_principal(),
        )


def test_pointer_target_precedence():
    from arabic_agent.tools.builtin.computer import _target_from_arguments

    by_element = _target_from_arguments({"element_id": "e1", "x": 5, "y": 6})
    assert by_element is not None and by_element.element_id == "e1"
    by_text = _target_from_arguments({"text": "حساب"})
    assert by_text is not None and by_text.text == "حساب"
    by_point = _target_from_arguments({"x": 5, "y": 6})
    assert by_point is not None and by_point.point is not None
    assert _target_from_arguments({}) is None


def test_grounding_target_from_arguments_roundtrip():
    target = GroundingTarget.at_point(3, 4)
    assert target.point is not None and target.point.as_tuple() == (3, 4)
