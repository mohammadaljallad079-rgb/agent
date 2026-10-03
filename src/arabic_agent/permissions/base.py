"""Permission vocabulary and policy data contracts."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import PermissionEffect, RiskLevel, StrEnum


class Permission(StrEnum):
    """Atomic capabilities a tool may require.

    Names are verbs over resources so they read well in audit logs and map
    cleanly onto future integrations.
    """

    # Filesystem
    READ_FILE = "READ_FILE"
    WRITE_FILE = "WRITE_FILE"
    MOVE_FILE = "MOVE_FILE"
    DELETE_FILE = "DELETE_FILE"
    LIST_DIRECTORY = "LIST_DIRECTORY"

    # Execution
    RUN_COMMAND = "RUN_COMMAND"

    # Physical / output
    PRINT_DOCUMENT = "PRINT_DOCUMENT"

    # Communications
    SEND_EMAIL = "SEND_EMAIL"
    SEND_WHATSAPP = "SEND_WHATSAPP"
    SEND_SOCIAL_MESSAGE = "SEND_SOCIAL_MESSAGE"

    # Business systems
    CREATE_INVOICE = "CREATE_INVOICE"
    DELETE_INVOICE = "DELETE_INVOICE"
    READ_ERP = "READ_ERP"
    WRITE_ERP = "WRITE_ERP"

    # Network / models
    NETWORK_ACCESS = "NETWORK_ACCESS"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Baseline risk for each permission, used when a tool does not override it.
_PERMISSION_RISK: dict[Permission, RiskLevel] = {
    Permission.READ_FILE: RiskLevel.LOW,
    Permission.LIST_DIRECTORY: RiskLevel.LOW,
    Permission.READ_ERP: RiskLevel.LOW,
    Permission.WRITE_FILE: RiskLevel.MEDIUM,
    Permission.MOVE_FILE: RiskLevel.MEDIUM,
    Permission.NETWORK_ACCESS: RiskLevel.MEDIUM,
    Permission.RUN_COMMAND: RiskLevel.HIGH,
    Permission.DELETE_FILE: RiskLevel.HIGH,
    Permission.PRINT_DOCUMENT: RiskLevel.MEDIUM,
    Permission.SEND_EMAIL: RiskLevel.HIGH,
    Permission.SEND_WHATSAPP: RiskLevel.HIGH,
    Permission.SEND_SOCIAL_MESSAGE: RiskLevel.HIGH,
    Permission.CREATE_INVOICE: RiskLevel.HIGH,
    Permission.DELETE_INVOICE: RiskLevel.CRITICAL,
    Permission.WRITE_ERP: RiskLevel.HIGH,
}


def default_permission_risk(permission: Permission) -> RiskLevel:
    """Return the baseline risk for a permission."""
    return _PERMISSION_RISK.get(permission, RiskLevel.MEDIUM)


class Principal(BaseModel):
    """An authenticated actor: a user, service, or automation within a tenant."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    user_id: str
    roles: frozenset[str] = frozenset()
    #: Optional explicit grant ceiling. If set, a permission not listed here is
    #: denied regardless of policy rules (least privilege at the principal).
    granted_permissions: frozenset[Permission] | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def may_hold(self, permission: Permission) -> bool:
        """Whether the principal is even eligible to be granted a permission."""
        return self.granted_permissions is None or permission in self.granted_permissions


class Rule(BaseModel):
    """A single ordered policy rule. First matching rule wins."""

    model_config = ConfigDict(frozen=True)

    effect: PermissionEffect
    reason: str = ""
    #: Restrict to principals holding this role (None = any role).
    role: str | None = None
    #: Restrict to a specific tool name (None = any tool).
    tool: str | None = None
    #: Restrict to a specific permission (None = any permission).
    permission: Permission | None = None
    #: Optional risk ceiling: rule only applies at or below this risk.
    max_risk: RiskLevel | None = None

    def matches(
        self,
        principal: Principal,
        tool_name: str,
        permission: Permission,
        risk: RiskLevel,
    ) -> bool:
        if self.role is not None and not principal.has_role(self.role):
            return False
        if self.tool is not None and self.tool != tool_name:
            return False
        if self.permission is not None and self.permission != permission:
            return False
        return not (self.max_risk is not None and _risk_rank(risk) > _risk_rank(self.max_risk))


_RISK_ORDER = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def _risk_rank(risk: RiskLevel) -> int:
    return _RISK_ORDER[risk]


class PolicyDecision(BaseModel):
    """The outcome of evaluating policy for one (tool, permission) pair."""

    model_config = ConfigDict(frozen=True)

    effect: PermissionEffect
    reason: str
    permission: Permission
    tool: str
    risk: RiskLevel = RiskLevel.MEDIUM
    matched_rule: Rule | None = None

    @property
    def allowed(self) -> bool:
        return self.effect is PermissionEffect.ALLOW

    @property
    def requires_approval(self) -> bool:
        return self.effect is PermissionEffect.REQUIRES_APPROVAL

    @property
    def denied(self) -> bool:
        return self.effect is PermissionEffect.DENY


class PolicySet(BaseModel):
    """A named, ordered set of rules for a tenant (or the platform default)."""

    model_config = ConfigDict(frozen=True)

    name: str
    rules: tuple[Rule, ...] = Field(default_factory=tuple)
    #: What to do when no rule matches. Deny-by-default is the safe choice.
    default_effect: PermissionEffect = PermissionEffect.DENY
