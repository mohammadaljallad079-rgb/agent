"""Generic connector contracts.

A *connector* is a real transport to an external system — an HTTP business API,
an SMTP server, a messaging provider, a webhook sink. It is deliberately **not**
a tool: a tool is the agent-facing action with a JSON schema and a permission
set; a connector is the provider-specific transport a tool is implemented *in
terms of*. Keeping them apart is what lets a new provider be added without the
agent core knowing anything about it.

Every connector declares:

* a stable :attr:`name` (used in config, audit and the tool surface);
* its :class:`ConnectorCapability` set, so the agent can discover what is
  available instead of guessing;
* its :class:`ConnectorKind` and, per operation, whether the operation reads or
  writes — a read-only connector can never be mistaken for a write-capable one;
* a health/authentication state, reported honestly (never "ready" when it is
  not).

The registry is the single place connectors are looked up, and it exposes the
capability catalogue the agent sees. A connector that is not configured or not
authenticated is still *discoverable* — the model is told it exists but is
unavailable, with a reason, rather than being left to guess.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ActionRisk, ConnectorKind, ConnectorState, StrEnum
from ..core.errors import ConfigurationError, NotFoundError
from ..core.redaction import redact


class ConnectorCapability(StrEnum):
    """A normalized capability a connector may expose.

    Capabilities are intentionally coarse and provider-neutral so the agent can
    reason about them: "this connector can read records and update them" rather
    than "this connector speaks this vendor's particular dialect".
    """

    # HTTP / generic
    HTTP_REQUEST = "http.request"
    # Email
    EMAIL_READ = "email.read"
    EMAIL_SEARCH = "email.search"
    EMAIL_DRAFT = "email.draft"
    EMAIL_SEND = "email.send"
    EMAIL_ATTACH = "email.attach"
    # Messaging
    MESSAGE_SEND = "message.send"
    MESSAGE_READ = "message.read"
    # Business / ERP / CRM
    RECORD_SEARCH = "record.search"
    RECORD_GET = "record.get"
    RECORD_CREATE = "record.create"
    RECORD_UPDATE = "record.update"
    ACTION_EXECUTE = "action.execute"
    # Webhooks
    WEBHOOK_SEND = "webhook.send"
    # Calendar
    CALENDAR_LIST = "calendar.list"
    CALENDAR_READ = "calendar.read"
    CALENDAR_CREATE = "calendar.create"
    CALENDAR_UPDATE = "calendar.update"
    CALENDAR_DELETE = "calendar.delete"
    CALENDAR_FREEBUSY = "calendar.freebusy"


class OperationKind(StrEnum):
    """Whether a connector operation reads or writes external state."""

    READ = "read"
    WRITE = "write"


class ConnectorOperation(BaseModel):
    """A single operation a connector supports.

    ``risk`` is the *classification* the permission/approval layer uses: a read
    is ``READ_ONLY``; an internal-note update is ``LOW_RISK_WRITE``; sending a
    customer email is ``CONSEQUENTIAL_WRITE``; a financial/delete action is
    ``HIGH_RISK_ACTION``.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    kind: OperationKind
    risk: ActionRisk
    description: str = ""


class ConnectorHealth(BaseModel):
    """An honest, redacted health snapshot of a connector."""

    model_config = ConfigDict(frozen=True)

    state: ConnectorState
    #: Whether the connector is configured at all (credentials/URL present).
    configured: bool = False
    #: Whether credentials are present and (best-effort) valid.
    authenticated: bool = False
    #: Human-readable, non-secret explanation of the current state.
    detail: str = ""
    #: Whether this connector can only read (never write) external state.
    read_only: bool = False

    @property
    def usable(self) -> bool:
        return self.state is ConnectorState.READY


class ConnectorInfo(BaseModel):
    """The model-facing capability/health description of one connector."""

    model_config = ConfigDict(frozen=True)

    name: str
    kind: ConnectorKind
    state: ConnectorState
    capabilities: tuple[ConnectorCapability, ...] = ()
    operations: tuple[ConnectorOperation, ...] = ()
    read_only: bool = False
    write_capable: bool = False
    approval_required: bool = False
    authenticated: bool = False
    detail: str = ""

    def as_prompt_line(self) -> str:
        """A compact, non-secret line the model can read."""
        caps = ", ".join(c.value for c in self.capabilities) or "none"
        flags = []
        if self.read_only:
            flags.append("read-only")
        if self.write_capable:
            flags.append("write-capable")
        if self.approval_required:
            flags.append("approval-required")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        detail = f" — {self.detail}" if self.detail else ""
        return f"- {self.name} ({self.kind.value}, {self.state.value}{flag_text}): {caps}{detail}"


class ConnectorResult(BaseModel):
    """A normalized connector response, redacted before it leaves the boundary."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, summary: str, data: dict[str, Any] | None = None) -> ConnectorResult:
        return cls(ok=True, summary=summary, data=redact(data or {}))

    @classmethod
    def failure(cls, summary: str, data: dict[str, Any] | None = None) -> ConnectorResult:
        return cls(ok=False, summary=summary, data=redact(data or {}))


class ConnectorCall(BaseModel):
    """A request to a connector, provider-neutral."""

    model_config = ConfigDict(frozen=True)

    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)


class Connector(ABC):
    """Base class for every external connector."""

    #: Stable identifier used in configuration, audit and the capability list.
    name: str
    #: What kind of external system this is (for grouping in the UI/agent).
    kind: ConnectorKind = ConnectorKind.HTTP
    #: Normalized capabilities this connector exposes.
    capabilities: tuple[ConnectorCapability, ...] = ()
    #: Per-operation read/write kind and risk classification.
    operations: tuple[ConnectorOperation, ...] = ()
    #: Whether this connector can only read (never write) external state.
    read_only: bool = False

    def __init__(self) -> None:
        if not getattr(self, "name", None):
            raise ValueError(f"{type(self).__name__} must define a name")

    # -- Discovery ------------------------------------------------------
    def operation(self, name: str) -> ConnectorOperation | None:
        for op in self.operations:
            if op.name == name:
                return op
        return None

    @property
    def write_capable(self) -> bool:
        return any(op.kind is OperationKind.WRITE for op in self.operations)

    @property
    def approval_required(self) -> bool:
        """Whether any operation is consequential enough to need approval."""
        return any(op.risk.requires_approval for op in self.operations)

    def health(self) -> ConnectorHealth:
        """Report the connector's state honestly. Override for real probing."""
        return ConnectorHealth(
            state=ConnectorState.UNAVAILABLE,
            configured=False,
            authenticated=False,
            read_only=self.read_only,
            detail=f"{self.name} does not report health",
        )

    def info(self) -> ConnectorInfo:
        health = self.health()
        return ConnectorInfo(
            name=self.name,
            kind=self.kind,
            state=health.state,
            capabilities=self.capabilities,
            operations=self.operations,
            read_only=self.read_only or not self.write_capable,
            write_capable=self.write_capable,
            approval_required=self.approval_required,
            authenticated=health.authenticated,
            detail=health.detail,
        )

    # -- Execution ------------------------------------------------------
    @abstractmethod
    async def call(self, call: ConnectorCall) -> ConnectorResult:
        """Perform an operation. Raise a PlatformError subclass on failure."""

    async def healthcheck(self) -> ConnectorHealth:
        """Actively verify reachability. Default: report the passive state."""
        return self.health()

    async def aclose(self) -> None:
        return None


class ConnectorRegistry:
    """Registry of connectors, keyed by name."""

    def __init__(self, connectors: list[Connector] | None = None) -> None:
        self._items: dict[str, Connector] = {}
        for connector in connectors or []:
            self.register(connector)

    def register(self, connector: Connector, *, replace: bool = False) -> None:
        if connector.name in self._items and not replace:
            raise ConfigurationError(f"connector already registered: {connector.name}")
        self._items[connector.name] = connector

    def get(self, name: str) -> Connector:
        try:
            return self._items[name]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown connector: {name}", details={"known": sorted(self._items)}
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)

    def all(self) -> tuple[Connector, ...]:
        return tuple(self._items[name] for name in self.names())

    def usable(self) -> tuple[Connector, ...]:
        """Only connectors whose health is ``READY``."""
        return tuple(c for c in self.all() if c.health().usable)

    def catalogue(self) -> tuple[ConnectorInfo, ...]:
        """The capability/health catalogue the agent is shown."""
        return tuple(c.info() for c in self.all())

    def catalogue_text(self) -> str:
        """A compact, non-secret catalogue for the system prompt."""
        lines = [info.as_prompt_line() for info in self.catalogue()]
        return "\n".join(lines) if lines else "No external connectors are configured."

    async def aclose(self) -> None:
        for connector in self._items.values():
            await connector.aclose()


__all__ = [
    "Connector",
    "ConnectorCall",
    "ConnectorCapability",
    "ConnectorHealth",
    "ConnectorInfo",
    "ConnectorOperation",
    "ConnectorRegistry",
    "ConnectorResult",
    "OperationKind",
]
