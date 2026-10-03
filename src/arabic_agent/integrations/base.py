"""Integration contracts.

Integrations are deliberately *not* tools. A tool is an agent-facing action with
a JSON schema; an integration is a transport to an external system. A tool such
as ``send_email`` is implemented *in terms of* an integration, which keeps the
provider-specific code (SMTP vs. API vs. Graph) out of the tool surface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ConfigurationError, NotFoundError
from ..core.redaction import redact


class IntegrationCall(BaseModel):
    """A request to an integration."""

    model_config = ConfigDict(frozen=True)

    operation: str
    payload: dict[str, Any] = Field(default_factory=dict)


class IntegrationResult(BaseModel):
    """An integration response, redacted before it leaves the boundary."""

    model_config = ConfigDict(frozen=True)

    ok: bool
    summary: str
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def success(cls, summary: str, data: dict[str, Any] | None = None) -> IntegrationResult:
        return cls(ok=True, summary=summary, data=redact(data or {}))

    @classmethod
    def failure(cls, summary: str, data: dict[str, Any] | None = None) -> IntegrationResult:
        return cls(ok=False, summary=summary, data=redact(data or {}))


class Integration(ABC):
    """Base class for external connectors."""

    #: Stable identifier used in configuration and audit records.
    name: str
    #: Operations this integration supports (for capability discovery).
    operations: tuple[str, ...] = ()
    #: Whether the connector is configured and ready to use.
    configured: bool = False

    @abstractmethod
    async def call(self, call: IntegrationCall) -> IntegrationResult:
        """Perform an operation. Raise a PlatformError subclass on failure."""

    async def healthcheck(self) -> IntegrationResult:
        """Verify the connector is reachable. Default: report configuration only."""
        if not self.configured:
            return IntegrationResult.failure(f"{self.name} is not configured")
        return IntegrationResult.success(f"{self.name} is configured")

    async def aclose(self) -> None:
        return None


class IntegrationRegistry:
    """Registry of available integrations, keyed by name."""

    def __init__(self) -> None:
        self._items: dict[str, Integration] = {}

    def register(self, integration: Integration, *, replace: bool = False) -> None:
        if integration.name in self._items and not replace:
            raise ConfigurationError(f"integration already registered: {integration.name}")
        self._items[integration.name] = integration

    def get(self, name: str) -> Integration:
        try:
            return self._items[name]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown integration: {name}", details={"known": sorted(self._items)}
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._items

    def names(self) -> list[str]:
        return sorted(self._items)

    def all(self) -> tuple[Integration, ...]:
        return tuple(self._items[name] for name in self.names())

    async def aclose(self) -> None:
        for integration in self._items.values():
            await integration.aclose()
