"""Tool abstraction.

A :class:`Tool` bundles a name, a model-facing JSON schema, the permissions it
requires, and an async handler. Handlers receive a :class:`ToolContext` (which
carries identity, limits and cancellation) and return a :class:`ToolResult`.
Handlers must raise :class:`PlatformError` subclasses on failure so the agent
can classify and recover.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import RiskLevel
from ..core.errors import ValidationError
from ..core.redaction import redact
from ..models.base import ToolSpec
from ..permissions.base import Permission, default_permission_risk


class ToolContext(BaseModel):
    """Everything a tool needs to know about the caller and its limits."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    tenant_id: str
    user_id: str
    run_id: str
    step_id: str | None = None
    #: Working directory for filesystem/command tools (the local runtime root).
    workspace_dir: str = "."
    #: Hard deadline for the tool call, in seconds.
    timeout_seconds: float = 30.0
    #: Free-form, non-secret metadata for integrations.
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """The outcome of a tool invocation, safe to feed back to a model.

    ``data`` is redacted before it leaves the tool boundary.
    """

    model_config = ConfigDict(frozen=True)

    ok: bool
    #: Concise, model-readable summary of what happened.
    summary: str
    #: Structured payload (redacted). Kept small to protect the context budget.
    data: dict[str, Any] = Field(default_factory=dict)
    #: Wall-clock duration in milliseconds, for observability.
    duration_ms: float = 0.0

    @classmethod
    def success(
        cls, summary: str, *, data: dict[str, Any] | None = None, duration_ms: float = 0.0
    ) -> ToolResult:
        return cls(ok=True, summary=summary, data=redact(data or {}), duration_ms=duration_ms)

    @classmethod
    def failure(
        cls, summary: str, *, data: dict[str, Any] | None = None, duration_ms: float = 0.0
    ) -> ToolResult:
        return cls(ok=False, summary=summary, data=redact(data or {}), duration_ms=duration_ms)


class Tool(ABC):
    """Base class for all tools.

    Subclasses set :attr:`name`, :attr:`description`, :attr:`parameters`,
    :attr:`permissions` and optionally :attr:`risks`, then implement
    :meth:`run`.
    """

    name: str
    description: str
    #: JSON schema for the arguments object.
    parameters: ClassVar[dict[str, Any]] = {"type": "object", "properties": {}}
    #: Permissions that must be granted before this tool may run.
    permissions: tuple[Permission, ...] = ()
    #: Per-permission risk overrides; unspecified permissions use the baseline.
    risks: ClassVar[dict[Permission, RiskLevel]] = {}
    #: Whether re-executing with identical arguments can cause a duplicate
    #: external effect (sending an email, creating an invoice, ...). Such tools
    #: are wrapped by the idempotency guard when one is configured, so a retried
    #: or redelivered execution does not perform the effect twice.
    side_effecting: ClassVar[bool] = False

    def __init__(self) -> None:
        if not getattr(self, "name", None):
            raise ValueError(f"{type(self).__name__} must define a name")
        if not getattr(self, "description", None):
            raise ValueError(f"{type(self).__name__} must define a description")

    # --- Introspection -----------------------------------------------------
    def spec(self) -> ToolSpec:
        """The model-facing description of this tool."""
        return ToolSpec(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
        )

    def permission_risks(self) -> dict[Permission, RiskLevel]:
        """Risk level for each required permission."""
        return {
            perm: self.risks.get(perm, default_permission_risk(perm)) for perm in self.permissions
        }

    def validate_arguments(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Validate arguments against the declared schema.

        Deliberately minimal (required-key + unknown-key checks) to avoid a
        hard dependency on a JSON-schema library; unknown keys are rejected so
        a model cannot smuggle unexpected fields into a handler.
        """
        if not isinstance(arguments, dict):
            raise ValidationError(f"{self.name}: arguments must be an object")
        required = self.parameters.get("required", []) or []
        properties = self.parameters.get("properties", {}) or {}
        missing = [key for key in required if key not in arguments]
        if missing:
            raise ValidationError(
                f"{self.name}: missing required argument(s): {', '.join(missing)}",
                details={"missing": missing},
            )
        if properties:
            unknown = [key for key in arguments if key not in properties]
            if unknown:
                raise ValidationError(
                    f"{self.name}: unknown argument(s): {', '.join(unknown)}",
                    details={"unknown": unknown},
                )
        return arguments

    # --- Execution ---------------------------------------------------------
    @abstractmethod
    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Execute the tool. Raise a :class:`PlatformError` on failure."""

    async def preview(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        """A non-destructive, structured preview of what this call would do.

        Called by the executor *only* when a call is about to require approval,
        so the human sees the concrete effect (which file, which text changes,
        how many replacements) rather than just the tool name. Returning
        ``None`` (the default) means the tool has no preview to add. An
        implementation must be read-only — it may inspect state but never
        mutate it — and must not raise: the executor treats a failing preview as
        "no preview" and still raises the approval.
        """
        return None

    async def execute(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        """Validate, time, and run the tool, converting stray errors.

        This wrapper guarantees the agent always receives a :class:`ToolResult`
        or a classified :class:`PlatformError` — never a raw exception.
        """
        validated = self.validate_arguments(arguments)
        started = time.perf_counter()
        try:
            result = await self.run(validated, context)
        except Exception as exc:
            from ..core.errors import PlatformError, ToolExecutionError

            if isinstance(exc, PlatformError):
                raise
            raise ToolExecutionError(
                f"{self.name} failed: {exc}", details={"tool": self.name}
            ) from exc
        duration_ms = (time.perf_counter() - started) * 1000.0
        if result.duration_ms:
            return result
        return result.model_copy(update={"duration_ms": duration_ms})
