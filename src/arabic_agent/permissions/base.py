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

    # Local filesystem capabilities (the desktop runtime's sandboxed surface).
    # These are distinct from the server-side file permissions above so a
    # tenant can grant local file access without granting server file access,
    # and vice versa.
    FILES_READ = "FILES_READ"
    FILES_WRITE = "FILES_WRITE"
    FILES_DELETE = "FILES_DELETE"
    FILES_MOVE = "FILES_MOVE"
    FILES_SEARCH = "FILES_SEARCH"

    # Local computer control (the desktop runtime's screen/perception/input
    # surface). SCREEN_READ and INPUT_CONTROL were reserved in an earlier phase;
    # POINTER_CONTROL / KEYBOARD_CONTROL / WINDOW_CONTROL split input control
    # into finer, independently grantable capabilities so reading the screen and
    # moving the pointer are not the same risk.
    CLIPBOARD_READ = "CLIPBOARD_READ"
    CLIPBOARD_WRITE = "CLIPBOARD_WRITE"
    SCREEN_READ = "SCREEN_READ"
    INPUT_CONTROL = "INPUT_CONTROL"
    POINTER_CONTROL = "POINTER_CONTROL"
    KEYBOARD_CONTROL = "KEYBOARD_CONTROL"
    WINDOW_CONTROL = "WINDOW_CONTROL"
    APPLICATION_LAUNCH = "APPLICATION_LAUNCH"
    APPLICATION_CLOSE = "APPLICATION_CLOSE"

    # Execution
    RUN_COMMAND = "RUN_COMMAND"

    # Physical / output
    PRINT_DOCUMENT = "PRINT_DOCUMENT"
    PRINT = "PRINT"

    # Communications
    SEND_EMAIL = "SEND_EMAIL"
    READ_EMAIL = "READ_EMAIL"
    SEND_WHATSAPP = "SEND_WHATSAPP"
    SEND_SOCIAL_MESSAGE = "SEND_SOCIAL_MESSAGE"
    SEND_WEBHOOK = "SEND_WEBHOOK"

    # External connectors (Phase 9). Coarse, provider-neutral grants: reading
    # external data, writing it, and generic outbound HTTP. A connector's own
    # declared risk decides whether a write additionally needs approval.
    CONNECTOR_READ = "CONNECTOR_READ"
    CONNECTOR_WRITE = "CONNECTOR_WRITE"
    CONNECTOR_HTTP = "CONNECTOR_HTTP"
    MANAGE_CONNECTOR = "MANAGE_CONNECTOR"

    # Business systems
    CREATE_INVOICE = "CREATE_INVOICE"
    DELETE_INVOICE = "DELETE_INVOICE"
    READ_ERP = "READ_ERP"
    WRITE_ERP = "WRITE_ERP"

    # Network / models
    NETWORK_ACCESS = "NETWORK_ACCESS"

    # Web / browser (the autonomous browser surface). Split so reading a page and
    # performing a consequential submission are not the same grant, and so a
    # tenant can allow research without allowing interaction or upload.
    BROWSER_NAVIGATE = "BROWSER_NAVIGATE"
    BROWSER_READ = "BROWSER_READ"
    BROWSER_INTERACT = "BROWSER_INTERACT"
    BROWSER_SUBMIT = "BROWSER_SUBMIT"
    BROWSER_DOWNLOAD = "BROWSER_DOWNLOAD"
    BROWSER_UPLOAD = "BROWSER_UPLOAD"

    # Workflow administration (defining/publishing/running workflows)
    MANAGE_WORKFLOW = "MANAGE_WORKFLOW"
    RUN_WORKFLOW = "RUN_WORKFLOW"

    # Calendar (the desktop/connector scheduling surface). Split so reading a
    # calendar, changing it, and removing events are independent grants: a tenant
    # can let the agent read availability without letting it move a meeting.
    CALENDAR_READ = "CALENDAR_READ"
    CALENDAR_WRITE = "CALENDAR_WRITE"
    CALENDAR_DELETE = "CALENDAR_DELETE"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


#: Baseline risk for each permission, used when a tool does not override it.
_PERMISSION_RISK: dict[Permission, RiskLevel] = {
    Permission.READ_FILE: RiskLevel.LOW,
    Permission.LIST_DIRECTORY: RiskLevel.LOW,
    Permission.READ_ERP: RiskLevel.LOW,
    Permission.FILES_READ: RiskLevel.LOW,
    Permission.FILES_SEARCH: RiskLevel.LOW,
    Permission.WRITE_FILE: RiskLevel.MEDIUM,
    Permission.MOVE_FILE: RiskLevel.MEDIUM,
    Permission.NETWORK_ACCESS: RiskLevel.MEDIUM,
    Permission.FILES_WRITE: RiskLevel.MEDIUM,
    Permission.FILES_MOVE: RiskLevel.MEDIUM,
    Permission.PRINT_DOCUMENT: RiskLevel.MEDIUM,
    Permission.PRINT: RiskLevel.MEDIUM,
    Permission.CLIPBOARD_READ: RiskLevel.MEDIUM,
    Permission.CLIPBOARD_WRITE: RiskLevel.MEDIUM,
    Permission.RUN_COMMAND: RiskLevel.HIGH,
    Permission.DELETE_FILE: RiskLevel.HIGH,
    Permission.FILES_DELETE: RiskLevel.HIGH,
    Permission.SCREEN_READ: RiskLevel.HIGH,
    Permission.POINTER_CONTROL: RiskLevel.HIGH,
    Permission.KEYBOARD_CONTROL: RiskLevel.HIGH,
    Permission.WINDOW_CONTROL: RiskLevel.MEDIUM,
    Permission.APPLICATION_LAUNCH: RiskLevel.HIGH,
    Permission.APPLICATION_CLOSE: RiskLevel.HIGH,
    Permission.SEND_EMAIL: RiskLevel.HIGH,
    Permission.READ_EMAIL: RiskLevel.MEDIUM,
    Permission.SEND_WHATSAPP: RiskLevel.HIGH,
    Permission.SEND_SOCIAL_MESSAGE: RiskLevel.HIGH,
    Permission.SEND_WEBHOOK: RiskLevel.HIGH,
    Permission.CONNECTOR_READ: RiskLevel.MEDIUM,
    Permission.CONNECTOR_WRITE: RiskLevel.HIGH,
    Permission.CONNECTOR_HTTP: RiskLevel.HIGH,
    Permission.MANAGE_CONNECTOR: RiskLevel.HIGH,
    Permission.CREATE_INVOICE: RiskLevel.HIGH,
    Permission.DELETE_INVOICE: RiskLevel.CRITICAL,
    Permission.WRITE_ERP: RiskLevel.HIGH,
    Permission.INPUT_CONTROL: RiskLevel.CRITICAL,
    Permission.BROWSER_NAVIGATE: RiskLevel.MEDIUM,
    Permission.BROWSER_READ: RiskLevel.MEDIUM,
    Permission.BROWSER_INTERACT: RiskLevel.HIGH,
    Permission.BROWSER_SUBMIT: RiskLevel.CRITICAL,
    Permission.BROWSER_DOWNLOAD: RiskLevel.MEDIUM,
    Permission.BROWSER_UPLOAD: RiskLevel.HIGH,
    Permission.MANAGE_WORKFLOW: RiskLevel.MEDIUM,
    Permission.RUN_WORKFLOW: RiskLevel.MEDIUM,
    Permission.CALENDAR_READ: RiskLevel.MEDIUM,
    Permission.CALENDAR_WRITE: RiskLevel.HIGH,
    Permission.CALENDAR_DELETE: RiskLevel.HIGH,
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
