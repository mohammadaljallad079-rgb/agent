"""Permission engine and approval store tests."""

from __future__ import annotations

import pytest

from arabic_agent.core.enums import ApprovalStatus, PermissionEffect
from arabic_agent.core.errors import NotFoundError
from arabic_agent.permissions.approvals import Approval, InMemoryApprovalStore
from arabic_agent.permissions.base import (
    Permission,
    PolicySet,
    Principal,
    Rule,
    default_permission_risk,
)
from arabic_agent.permissions.policy import PolicyEngine


def _principal(**kwargs) -> Principal:
    defaults: dict = {"tenant_id": "tnt_1", "user_id": "usr_1"}
    defaults.update(kwargs)
    return Principal(**defaults)


def _evaluate(engine: PolicyEngine, principal: Principal, tool: str, permission: Permission):
    return engine.evaluate(
        principal,
        tool_name=tool,
        permission=permission,
        risk=default_permission_risk(permission),
    )


def test_read_file_allowed_by_default():
    decision = _evaluate(PolicyEngine(), _principal(), "read_file", Permission.READ_FILE)
    assert decision.effect is PermissionEffect.ALLOW
    assert decision.allowed


def test_delete_file_denied_by_default():
    decision = _evaluate(PolicyEngine(), _principal(), "delete_file", Permission.DELETE_FILE)
    assert decision.effect is PermissionEffect.DENY
    assert decision.denied


def test_write_file_requires_approval_by_default():
    decision = _evaluate(PolicyEngine(), _principal(), "write_file", Permission.WRITE_FILE)
    assert decision.effect is PermissionEffect.REQUIRES_APPROVAL
    assert decision.requires_approval


def test_permission_ceiling_denies_unlisted_permission():
    principal = _principal(granted_permissions=frozenset({Permission.READ_FILE}))
    decision = _evaluate(PolicyEngine(), principal, "write_file", Permission.WRITE_FILE)
    assert decision.denied
    assert "not granted" in decision.reason


def test_tenant_policy_override_is_isolated():
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_2",
        PolicySet(
            name="tnt_2",
            rules=(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.WRITE_FILE,
                    reason="trusted tenant",
                ),
            ),
        ),
    )
    allowed = _evaluate(engine, _principal(tenant_id="tnt_2"), "write_file", Permission.WRITE_FILE)
    other = _evaluate(engine, _principal(tenant_id="tnt_1"), "write_file", Permission.WRITE_FILE)
    assert allowed.allowed
    assert other.requires_approval


def test_role_scoped_rule_only_applies_to_that_role():
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="tnt_1",
            rules=(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    role="admin",
                    permission=Permission.RUN_COMMAND,
                    reason="admins may run commands",
                ),
            ),
            default_effect=PermissionEffect.REQUIRES_APPROVAL,
        ),
    )
    admin = _evaluate(
        engine, _principal(roles=frozenset({"admin"})), "run_command", Permission.RUN_COMMAND
    )
    member = _evaluate(
        engine, _principal(roles=frozenset({"member"})), "run_command", Permission.RUN_COMMAND
    )
    assert admin.allowed
    assert member.requires_approval


async def test_approval_lifecycle():
    store = InMemoryApprovalStore()
    approval = Approval.create(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        tool_name="write_file",
        permission=Permission.WRITE_FILE,
        summary="write a file",
        arguments={"path": "a.txt", "content": "hi"},
    )
    await store.add(approval)
    assert await store.list_pending(tenant_id="tnt_1") == [approval]

    resolved = await store.resolve(
        approval.id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    assert resolved.status is ApprovalStatus.APPROVED
    assert await store.list_pending(tenant_id="tnt_1") == []


async def test_approval_arguments_are_redacted():
    approval = Approval.create(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        tool_name="send_email",
        permission=Permission.SEND_EMAIL,
        summary="send",
        arguments={"api_key": "sk-abcdefghijklmnopqrstuvwx", "to": "a@b.c"},
    )
    assert approval.arguments["api_key"] == "***REDACTED***"
    assert approval.arguments["to"] == "a@b.c"


async def test_cross_tenant_approval_access_is_denied():
    store = InMemoryApprovalStore()
    approval = Approval.create(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        tool_name="write_file",
        permission=Permission.WRITE_FILE,
        summary="write",
    )
    await store.add(approval)
    with pytest.raises(NotFoundError):
        await store.get(approval.id, tenant_id="tnt_2")


async def test_resolving_twice_is_rejected():
    store = InMemoryApprovalStore()
    approval = Approval.create(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        tool_name="write_file",
        permission=Permission.WRITE_FILE,
        summary="write",
    )
    await store.add(approval)
    await store.resolve(
        approval.id, tenant_id="tnt_1", status=ApprovalStatus.APPROVED, resolved_by="usr_1"
    )
    with pytest.raises(NotFoundError):
        await store.resolve(
            approval.id, tenant_id="tnt_1", status=ApprovalStatus.DENIED, resolved_by="usr_1"
        )
