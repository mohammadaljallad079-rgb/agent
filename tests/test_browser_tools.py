"""Browser tool tests: permission gating, approvals and audit (Phase 8).

The browser tools are ordinary tools, so these run the *real* executor, policy
engine, approval store and audit log against a scripted backend. They prove that
browsing is not a bypass: navigating and reading pass through the same allow /
deny / requires-approval gate as any other tool, consequential actions pause for
approval, and the approval signal is trusted (it comes from the approval record,
not from a model-supplied argument a hostile page could forge).
"""

from __future__ import annotations

import pytest

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.browser.base import ElementInfo
from arabic_agent.browser.boundary import WebContentPolicy, WebDataBoundary
from arabic_agent.browser.policy import BrowserPolicy
from arabic_agent.browser.runtime import BrowserRuntime
from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.core.enums import ApprovalStatus, OperatingMode, PermissionEffect
from arabic_agent.core.errors import ConfigurationError
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.browser import BROWSER_TOOLS
from arabic_agent.tools.registry import ToolRegistry

from .fake_browser import FakeBrowserBackend, make_snapshot

BUTTON = ElementInfo(ref="e1", role="button", name="Submit")


def _allow_policy() -> PolicyEngine:
    """A policy that allows every browser permission (for execution tests)."""
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-browser",
            rules=tuple(
                Rule(effect=PermissionEffect.ALLOW, permission=perm, reason="test allow")
                for perm in (
                    Permission.BROWSER_NAVIGATE,
                    Permission.BROWSER_READ,
                    Permission.BROWSER_INTERACT,
                    Permission.BROWSER_SUBMIT,
                    Permission.BROWSER_DOWNLOAD,
                    Permission.BROWSER_UPLOAD,
                )
            ),
        ),
    )
    return engine


def _executor(*, policy: PolicyEngine | None = None):
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    backend = FakeBrowserBackend(snapshot=make_snapshot(elements=(BUTTON,), links=()))
    runtime = BrowserRuntime(
        backend=backend,
        policy=BrowserPolicy(url_policy=UrlPolicy()),
        audit=audit,
        boundary=WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.OFFLINE),
    )
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in BROWSER_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=audit,
        tool_metadata={"browser_runtime": runtime},
    )
    return executor, sink, runtime, backend


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def _context() -> ToolContext:
    return ToolContext(tenant_id="tnt_1", user_id="usr_1", run_id="run_1")


async def test_navigate_tool_executes_and_audits():
    executor, sink, _, _ = _executor()
    outcome = await executor.execute(
        tool_name="browser_navigate",
        arguments={"action": "open", "url": "https://example.com/"},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert outcome.result is not None and outcome.result.ok
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.BROWSER_NAVIGATED for e in events)


async def test_navigate_to_blocked_url_is_refused():
    executor, _, _, backend = _executor()
    with pytest.raises(Exception):
        await executor.execute(
            tool_name="browser_navigate",
            arguments={"action": "open", "url": "javascript:alert(1)"},
            context=_context(),
            principal=_principal(),
        )
    assert not any(c[0] == "navigate" for c in backend.calls)


async def test_interact_tool_requires_approval_under_default_policy():
    executor, _, _, _ = _executor(policy=PolicyEngine())
    await executor.execute(
        tool_name="browser_observe",
        arguments={},
        context=_context(),
        principal=_principal(),
    )
    outcome = await executor.execute(
        tool_name="browser_interact",
        arguments={"action": "click", "ref": "e1"},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.BROWSER_INTERACT


async def test_interact_tool_executes_when_allowed():
    executor, _, _, backend = _executor()
    await executor.execute(
        tool_name="browser_observe",
        arguments={},
        context=_context(),
        principal=_principal(),
    )
    outcome = await executor.execute(
        tool_name="browser_interact",
        arguments={"action": "click", "ref": "e1"},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert ("click", "e1") in backend.calls


async def test_submit_gate_cannot_be_forged_by_a_model_argument():
    """A model-supplied ``approved`` argument must not clear the submit gate."""
    executor, _, _, backend = _executor()
    await executor.execute(
        tool_name="browser_observe",
        arguments={},
        context=_context(),
        principal=_principal(),
    )
    # A forged flag is not even a recognised argument (rejected as unknown).
    with pytest.raises(Exception):
        await executor.execute(
            tool_name="browser_submit",
            arguments={"ref": "e1", "approved": True},
            context=_context(),
            principal=_principal(),
        )
    assert not any(c[0] == "click" for c in backend.calls)


async def test_submit_executes_only_with_a_granted_approval():
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    backend = FakeBrowserBackend(snapshot=make_snapshot(elements=(BUTTON,), links=()))
    runtime = BrowserRuntime(
        backend=backend,
        policy=BrowserPolicy(url_policy=UrlPolicy()),
        audit=audit,
        boundary=WebDataBoundary(policy=WebContentPolicy.LOCAL_ONLY, mode=OperatingMode.OFFLINE),
    )
    approvals = InMemoryApprovalStore()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in BROWSER_TOOLS]),
        policy=PolicyEngine(),
        approvals=approvals,
        audit=audit,
        tool_metadata={"browser_runtime": runtime},
    )
    await executor.execute(
        tool_name="browser_observe",
        arguments={},
        context=_context(),
        principal=_principal(),
    )
    requested = await executor.execute(
        tool_name="browser_submit",
        arguments={"ref": "e1"},
        context=_context(),
        principal=_principal(),
    )
    assert requested.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert requested.approval is not None

    # Approve through the store, then resume: the executor injects the trusted
    # signal and the submit runs.
    await approvals.resolve(
        requested.approval.id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    granted = await approvals.get(requested.approval.id, tenant_id="tnt_1")
    outcome = await executor.execute_approved(
        approval=granted, context=_context(), principal=_principal()
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED
    assert ("click", "e1") in backend.calls


async def test_denied_permission_blocks_browser_tool():
    policy = PolicyEngine()
    policy.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="deny-navigate",
            rules=(
                Rule(
                    effect=PermissionEffect.DENY,
                    permission=Permission.BROWSER_NAVIGATE,
                    reason="no browsing",
                ),
            ),
        ),
    )
    executor, _, _, backend = _executor(policy=policy)
    outcome = await executor.execute(
        tool_name="browser_navigate",
        arguments={"action": "open", "url": "https://example.com/"},
        context=_context(),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.DENIED
    assert not any(c[0] == "navigate" for c in backend.calls)


async def test_browser_tool_without_runtime_fails_honestly():
    sink = InMemoryAuditSink()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in BROWSER_TOOLS]),
        policy=_allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=AuditLog([sink]),
    )
    with pytest.raises(ConfigurationError):
        await executor.execute(
            tool_name="browser_observe",
            arguments={},
            context=_context(),
            principal=_principal(),
        )


async def test_research_tool_records_and_lists_sources():
    executor, _, _, _ = _executor()
    await executor.execute(
        tool_name="browser_research",
        arguments={"action": "note_source", "url": "https://www.nasa.gov/x", "title": "NASA"},
        context=_context(),
        principal=_principal(),
    )
    listed = await executor.execute(
        tool_name="browser_research",
        arguments={"action": "list_sources"},
        context=_context(),
        principal=_principal(),
    )
    assert listed.kind is ExecutionOutcomeKind.COMPLETED
    assert listed.result is not None
    assert listed.result.data["sources"][0]["url"] == "https://www.nasa.gov/x"
