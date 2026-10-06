"""Connector runtime + tool execution tests (Phase 9).

These run the *real* executor, policy engine, approval store and audit log, with
the connector tools wired to a real :class:`ConnectorRuntime` over real local
transports. They prove that external actions are not a bypass: reads pass the
gate, consequential writes pause for a trusted approval, an approval is bound to
the exact payload, a retry never repeats an effect, and an ambiguous timeout is
never blindly retried.
"""

from __future__ import annotations

import pytest

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.browser.url_security import UrlPolicy
from arabic_agent.connectors.email import EmailConnector
from arabic_agent.connectors.http import HttpConnector
from arabic_agent.connectors.ledger import InMemoryActionLedger, canonical_digest
from arabic_agent.connectors.messaging import (
    DeliveryResult,
    MessagingConnector,
    MessagingMessage,
    MessagingProvider,
)
from arabic_agent.connectors.runtime import ConnectorRuntime
from arabic_agent.core.enums import (
    ActionState,
    ApprovalStatus,
    ConnectorState,
    DataSensitivity,
    OperatingMode,
    PermissionEffect,
)
from arabic_agent.core.errors import (
    AmbiguousActionError,
    ConfigurationError,
    PermissionDeniedError,
    TransientError,
)
from arabic_agent.local.boundary import DataBoundaryPolicy
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.connectors import CONNECTOR_TOOLS
from arabic_agent.tools.registry import ToolRegistry

from .fake_http import http_server


class FakeMessagingProvider(MessagingProvider):
    """A provider whose send/verify behaviour the test scripts."""

    name = "fake"

    def __init__(self, *, verify_status: str = "delivered", fail: bool = False) -> None:
        self.sent: list[MessagingMessage] = []
        self._verify_status = verify_status
        self._fail = fail

    async def send(self, message: MessagingMessage) -> DeliveryResult:
        if self._fail:
            raise TransientError("provider unreachable")
        self.sent.append(message)
        return DeliveryResult(provider_message_id="pm-1", status="accepted")

    async def read(self, *, conversation_id: str, limit: int) -> list[MessagingMessage]:
        return []

    async def verify(self, provider_message_id: str) -> DeliveryResult:
        accepted = self._verify_status == "delivered"
        return DeliveryResult(
            provider_message_id=provider_message_id,
            status=self._verify_status,
            accepted=accepted,
        )


def _allow_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-connectors",
            rules=tuple(
                Rule(effect=PermissionEffect.ALLOW, permission=perm, reason="test allow")
                for perm in (
                    Permission.CONNECTOR_READ,
                    Permission.CONNECTOR_WRITE,
                    Permission.CONNECTOR_HTTP,
                    Permission.READ_EMAIL,
                    Permission.SEND_EMAIL,
                    Permission.SEND_WEBHOOK,
                    Permission.SEND_WHATSAPP,
                )
            ),
        ),
    )
    return engine


def _runtime(
    *,
    registry,
    audit: AuditLog,
    boundary: DataBoundaryPolicy | None = None,
    ledger: InMemoryActionLedger | None = None,
) -> ConnectorRuntime:
    return ConnectorRuntime(
        registry=registry,
        audit=audit,
        ledger=ledger or InMemoryActionLedger(),
        boundary=boundary,
    )


def _executor(runtime: ConnectorRuntime, *, policy: PolicyEngine | None = None):
    sink = InMemoryAuditSink()
    audit = runtime._audit  # reuse the runtime's log so we observe its events
    approvals = InMemoryApprovalStore()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in CONNECTOR_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=approvals,
        audit=audit,
        tool_metadata={"connector_runtime": runtime},
    )
    return executor, sink, approvals


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def _context() -> ToolContext:
    return ToolContext(tenant_id="tnt_1", user_id="usr_1", run_id="run_1")


def _http_registry(server, *, name: str = "api"):
    from arabic_agent.connectors.base import ConnectorRegistry

    connector = HttpConnector(
        name=name,
        base_url=server.base_url,
        url_policy=UrlPolicy(allow_local_addresses=True),
    )
    registry = ConnectorRegistry()
    registry.register(connector)
    return registry


# --------------------------------------------------------------------------
# Reads pass, consequential writes pause for approval
# --------------------------------------------------------------------------
async def test_connector_list_tool_reports_catalogue():
    with http_server() as server:
        sink = InMemoryAuditSink()
        runtime = _runtime(registry=_http_registry(server), audit=AuditLog([sink]))
        executor, _, _ = _executor(runtime)
        outcome = await executor.execute(
            tool_name="connector_list", arguments={}, context=_context(), principal=_principal()
        )
        assert outcome.kind is ExecutionOutcomeKind.COMPLETED
        assert outcome.result is not None
        assert outcome.result.data["count"] == 1


async def test_http_request_tool_requires_approval_under_default_policy():
    with http_server() as server:
        sink = InMemoryAuditSink()
        runtime = _runtime(registry=_http_registry(server), audit=AuditLog([sink]))
        executor, _, _ = _executor(runtime, policy=PolicyEngine())
        outcome = await executor.execute(
            tool_name="http_request",
            arguments={"connector": "api", "method": "GET", "path": "/x"},
            context=_context(),
            principal=_principal(),
        )
        assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
        assert outcome.approval is not None
        assert outcome.approval.permission is Permission.CONNECTOR_HTTP
        assert server.requests == []


async def test_http_request_executes_only_after_granted_approval():
    with http_server() as server:
        server.route("/x", body='{"ok": true}')
        sink = InMemoryAuditSink()
        runtime = _runtime(registry=_http_registry(server), audit=AuditLog([sink]))
        executor, _, approvals = _executor(runtime, policy=PolicyEngine())
        requested = await executor.execute(
            tool_name="http_request",
            arguments={"connector": "api", "method": "GET", "path": "/x"},
            context=_context(),
            principal=_principal(),
        )
        assert requested.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
        assert requested.approval is not None
        assert server.requests == []
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
        assert server.requests[0].method == "GET"
        events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
        assert any(e.type is AuditEventType.EXTERNAL_ACTION_SUCCEEDED for e in events)


async def test_approval_digest_must_match_the_executed_payload():
    """The runtime refuses an approval bound to a different payload digest."""
    provider = FakeMessagingProvider()
    runtime, sink, _ = await _messaging_runtime(provider)
    payload = {"recipient": "+100", "body": "hi"}
    wrong = canonical_digest(
        connector="msg", operation="message.send", payload={"recipient": "+999", "body": "other"}
    )
    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(
            connector_name="msg",
            operation="message.send",
            payload=payload,
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
            approval_granted=True,
            approved_digest=wrong,
        )
    assert provider.sent == []
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.EXTERNAL_ACTION_APPROVAL_MISMATCH for e in events)

    correct = canonical_digest(connector="msg", operation="message.send", payload=payload)
    invocation = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        approval_granted=True,
        approved_digest=correct,
    )
    assert invocation.ok
    assert len(provider.sent) == 1


# --------------------------------------------------------------------------
# Consequential action: ledger, verification, dedupe, ambiguity
# --------------------------------------------------------------------------
async def _messaging_runtime(provider: FakeMessagingProvider):
    from arabic_agent.connectors.base import ConnectorRegistry

    connector = MessagingConnector(name="msg", provider=provider)
    registry = ConnectorRegistry()
    registry.register(connector)
    sink = InMemoryAuditSink()
    ledger = InMemoryActionLedger()
    runtime = _runtime(registry=registry, audit=AuditLog([sink]), ledger=ledger)
    return runtime, sink, ledger


async def test_consequential_send_requires_approval_and_records_ledger():
    provider = FakeMessagingProvider()
    runtime, _, ledger = await _messaging_runtime(provider)
    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(
            connector_name="msg",
            operation="message.send",
            payload={"recipient": "+100", "body": "hi"},
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
        )
    assert provider.sent == []

    invocation = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload={"recipient": "+100", "body": "hi"},
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        approval_granted=True,
    )
    assert invocation.ok
    assert len(provider.sent) == 1
    assert invocation.action_id is not None
    record = await ledger.get(invocation.action_id, tenant_id="tnt_1")
    assert record.state in (ActionState.SUCCEEDED, ActionState.VERIFIED)


async def test_identical_action_is_not_repeated():
    provider = FakeMessagingProvider()
    runtime, _, _ = await _messaging_runtime(provider)
    payload = {"recipient": "+100", "body": "hi"}
    first = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        step_id="step-1",
        approval_granted=True,
    )
    assert first.ok
    second = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        step_id="step-1",
        approval_granted=True,
    )
    assert second.ok
    assert second.deduplicated
    assert len(provider.sent) == 1


async def test_consequential_action_is_verified_against_provider():
    provider = FakeMessagingProvider(verify_status="delivered")
    runtime, _, _ = await _messaging_runtime(provider)

    async def verifier(data: dict) -> tuple[bool, str]:
        delivery = await provider.verify(str(data.get("provider_message_id") or ""))
        return delivery.accepted, f"status {delivery.status}"

    invocation = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload={"recipient": "+100", "body": "hi"},
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        approval_granted=True,
        verifier=verifier,
    )
    assert invocation.ok
    assert invocation.verified


async def test_verification_failure_marks_action_unknown():
    provider = FakeMessagingProvider(verify_status="unknown")
    runtime, sink, _ = await _messaging_runtime(provider)

    async def verifier(data: dict) -> tuple[bool, str]:
        return False, "provider cannot confirm"

    invocation = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload={"recipient": "+100", "body": "hi"},
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        approval_granted=True,
        verifier=verifier,
    )
    assert invocation.ok
    assert not invocation.verified
    assert invocation.state is ActionState.UNKNOWN
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.EXTERNAL_ACTION_UNKNOWN for e in events)


async def test_ambiguous_timeout_marks_unknown_and_never_repeats():
    provider = FakeMessagingProvider(fail=True)
    runtime, _, _ = await _messaging_runtime(provider)
    payload = {"recipient": "+100", "body": "hi"}
    first = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        step_id="step-1",
        approval_granted=True,
    )
    assert not first.ok
    assert first.state is ActionState.UNKNOWN
    # A retry must be refused, not silently repeated.
    second = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        step_id="step-1",
        approval_granted=True,
    )
    assert not second.ok
    assert second.data.get("unresolved") is True


async def test_boundary_refuses_local_only_payload_to_remote():
    provider = FakeMessagingProvider()
    runtime, _, _ = await _messaging_runtime(provider)
    runtime._boundary = DataBoundaryPolicy(mode=OperatingMode.OFFLINE)
    with pytest.raises(PermissionDeniedError):
        await runtime.invoke(
            connector_name="msg",
            operation="message.send",
            payload={"recipient": "+100", "body": "hi"},
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
            approval_granted=True,
            sensitivity=DataSensitivity.CONFIDENTIAL,
        )
    assert provider.sent == []


async def test_unavailable_connector_is_reported_honestly():
    from arabic_agent.connectors.base import ConnectorRegistry

    connector = EmailConnector(name="email")  # no host configured
    registry = ConnectorRegistry()
    registry.register(connector)
    sink = InMemoryAuditSink()
    runtime = _runtime(registry=registry, audit=AuditLog([sink]))
    with pytest.raises(ConfigurationError):
        await runtime.invoke(
            connector_name="email",
            operation="email.search",
            payload={"query": "ALL"},
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
        )
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.CONNECTOR_UNAVAILABLE for e in events)


async def test_connector_tool_without_runtime_fails_honestly():
    sink = InMemoryAuditSink()
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in CONNECTOR_TOOLS]),
        policy=_allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=AuditLog([sink]),
    )
    with pytest.raises(ConfigurationError):
        await executor.execute(
            tool_name="connector_list", arguments={}, context=_context(), principal=_principal()
        )


async def test_denied_permission_blocks_connector_tool():
    with http_server() as server:
        sink = InMemoryAuditSink()
        runtime = _runtime(registry=_http_registry(server), audit=AuditLog([sink]))
        policy = PolicyEngine()
        policy.set_tenant_policy(
            "tnt_1",
            PolicySet(
                name="deny-http",
                rules=(
                    Rule(
                        effect=PermissionEffect.DENY,
                        permission=Permission.CONNECTOR_HTTP,
                        reason="no outbound",
                    ),
                ),
            ),
        )
        executor, _, _ = _executor(runtime, policy=policy)
        outcome = await executor.execute(
            tool_name="http_request",
            arguments={"connector": "api", "method": "GET", "path": "/x"},
            context=_context(),
            principal=_principal(),
        )
        assert outcome.kind is ExecutionOutcomeKind.DENIED
        assert server.requests == []


async def test_verify_action_confirms_a_provider_delivery():
    # The send times out (ambiguous -> UNKNOWN); verification then confirms the
    # provider actually delivered it, without repeating the send.
    provider = FakeMessagingProvider(fail=True, verify_status="delivered")
    runtime, _, _ = await _messaging_runtime(provider)
    payload = {"recipient": "+100", "body": "hi"}
    invocation = await runtime.invoke(
        connector_name="msg",
        operation="message.send",
        payload=payload,
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        approval_granted=True,
    )
    assert not invocation.ok
    assert invocation.state is ActionState.UNKNOWN

    async def verifier(data: dict) -> tuple[bool, str]:
        delivery = await provider.verify(str(data.get("provider_request_id") or ""))
        return delivery.accepted, f"status {delivery.status}"

    verified, evidence = await runtime.verify_action(
        action_id=invocation.action_id,
        tenant_id="tnt_1",
        verifier=verifier,
    )
    assert verified
    assert "delivered" in evidence


def test_ambiguous_action_error_is_not_retryable():
    error = AmbiguousActionError("timeout")
    assert error.retryable is False


def test_connector_state_enum_values():
    assert ConnectorState.READY.value == "ready"
    assert ConnectorState.REAUTHENTICATION_REQUIRED.value == "reauthentication_required"
