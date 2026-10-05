"""Policy engine.

Evaluates whether a principal may exercise a permission on a tool. Rules are
ordered and first-match-wins; unmatched requests fall through to the policy
set's default effect (deny by default).
"""

from __future__ import annotations

from ..core.enums import PermissionEffect, RiskLevel
from .base import (
    Permission,
    PolicyDecision,
    PolicySet,
    Principal,
    Rule,
)


def platform_default_policy() -> PolicySet:
    """The safe baseline policy applied when a tenant defines no overrides.

    Read-only, low-risk operations are allowed; anything that mutates state,
    communicates externally, or runs commands requires explicit human approval;
    destructive business operations (deleting invoices) are denied outright and
    must be opted into per-tenant.
    """
    return PolicySet(
        name="platform-default",
        default_effect=PermissionEffect.DENY,
        rules=(
            # Read-only access is safe to allow by default.
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.READ_FILE,
                reason="read-only filesystem access",
            ),
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.LIST_DIRECTORY,
                reason="directory listing is read-only",
            ),
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.FILES_READ,
                reason="local file reads are read-only",
            ),
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.FILES_SEARCH,
                reason="local file search is read-only",
            ),
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.READ_ERP,
                reason="read-only ERP access",
            ),
            # Workflows orchestrate existing tools; they never bypass policy.
            # Defining/publishing a workflow and starting a workflow run are
            # allowed for an authenticated principal, while every *step* that
            # touches a tool is still gated by that tool's own permissions.
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.MANAGE_WORKFLOW,
                reason="workflow authoring is governed by the caller's RBAC",
            ),
            Rule(
                effect=PermissionEffect.ALLOW,
                permission=Permission.RUN_WORKFLOW,
                reason="workflow execution is governed by per-step tool policy",
            ),
            # Mutating or outward-facing actions need a human in the loop.
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.WRITE_FILE,
                reason="writing files can overwrite user data",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.FILES_WRITE,
                reason="writing local files can overwrite user data",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.FILES_MOVE,
                reason="moving local files changes user data layout",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.CLIPBOARD_READ,
                reason="reading the clipboard can expose sensitive data",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.CLIPBOARD_WRITE,
                reason="writing the clipboard is externally visible to other apps",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.MOVE_FILE,
                reason="moving files changes user data layout",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.RUN_COMMAND,
                reason="running shell commands is high risk",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.NETWORK_ACCESS,
                reason="outbound network access is high risk",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.SEND_EMAIL,
                reason="sending email is externally visible",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.SEND_WHATSAPP,
                reason="sending WhatsApp is externally visible",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.SEND_SOCIAL_MESSAGE,
                reason="posting to social media is externally visible",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.CREATE_INVOICE,
                reason="creating invoices affects accounting records",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.WRITE_ERP,
                reason="writing to the ERP affects business records",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.PRINT_DOCUMENT,
                reason="printing is a physical side effect",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.PRINT,
                reason="printing is a physical side effect",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.SCREEN_READ,
                reason="screen capture can expose anything on the display",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.APPLICATION_LAUNCH,
                reason="launching applications changes the user's machine",
            ),
            Rule(
                effect=PermissionEffect.REQUIRES_APPROVAL,
                permission=Permission.APPLICATION_CLOSE,
                reason="closing applications can lose unsaved work",
            ),
            # Destructive, irreversible operations are denied by default.
            Rule(
                effect=PermissionEffect.DENY,
                permission=Permission.DELETE_FILE,
                reason="deleting files is irreversible",
            ),
            Rule(
                effect=PermissionEffect.DENY,
                permission=Permission.FILES_DELETE,
                reason="deleting local files is irreversible",
            ),
            Rule(
                effect=PermissionEffect.DENY,
                permission=Permission.INPUT_CONTROL,
                reason="synthetic keyboard/mouse control is critical risk",
            ),
            Rule(
                effect=PermissionEffect.DENY,
                permission=Permission.DELETE_INVOICE,
                reason="deleting invoices is irreversible and audited",
            ),
        ),
    )


class PolicyEngine:
    """Resolves policy decisions for principals against tool permissions."""

    def __init__(self, default_policy: PolicySet | None = None) -> None:
        self._default = default_policy or platform_default_policy()
        self._tenant_policies: dict[str, PolicySet] = {}

    @property
    def default_policy(self) -> PolicySet:
        return self._default

    def set_tenant_policy(self, tenant_id: str, policy: PolicySet) -> None:
        self._tenant_policies[tenant_id] = policy

    def policy_for(self, tenant_id: str) -> PolicySet:
        return self._tenant_policies.get(tenant_id, self._default)

    def evaluate(
        self,
        principal: Principal,
        *,
        tool_name: str,
        permission: Permission,
        risk: RiskLevel,
    ) -> PolicyDecision:
        """Decide whether ``principal`` may exercise ``permission`` on ``tool``."""
        # Least privilege: the principal itself may not be eligible at all.
        if not principal.may_hold(permission):
            return PolicyDecision(
                effect=PermissionEffect.DENY,
                reason="permission not granted to principal",
                permission=permission,
                tool=tool_name,
                risk=risk,
            )

        policy = self.policy_for(principal.tenant_id)
        for rule in policy.rules:
            if rule.matches(principal, tool_name, permission, risk):
                return PolicyDecision(
                    effect=rule.effect,
                    reason=rule.reason or f"matched rule for {permission.value}",
                    permission=permission,
                    tool=tool_name,
                    risk=risk,
                    matched_rule=rule,
                )
        return PolicyDecision(
            effect=policy.default_effect,
            reason=f"no rule matched; default effect is {policy.default_effect.value}",
            permission=permission,
            tool=tool_name,
            risk=risk,
        )

    def evaluate_all(
        self,
        principal: Principal,
        *,
        tool_name: str,
        permissions: tuple[Permission, ...],
        risks: dict[Permission, RiskLevel],
    ) -> list[PolicyDecision]:
        """Evaluate every permission a tool requires, in declaration order."""
        return [
            self.evaluate(
                principal,
                tool_name=tool_name,
                permission=perm,
                risk=risks.get(perm, RiskLevel.MEDIUM),
            )
            for perm in permissions
        ]
