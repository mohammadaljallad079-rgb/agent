"""Connector policy: which connectors are enabled and how risky each is.

The runtime decides allow/deny from the *declared* risk of an operation; this
policy lets an operator narrow that further without editing code: disable a
connector entirely, force approval on an otherwise-low-risk write, or pin a
connector's data as local-only (never transmitted to an external SaaS). It is a
pure, declarative object so the decision is testable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ActionRisk, DataSensitivity


class ConnectorPolicy(BaseModel):
    """Per-connector governance overrides."""

    model_config = ConfigDict(frozen=True)

    #: Connectors that are enabled at all. Empty means "every registered one".
    enabled: tuple[str, ...] = ()
    #: Connectors explicitly disabled (wins over ``enabled``).
    disabled: tuple[str, ...] = ()
    #: Operations forced to require approval regardless of declared risk, as
    #: ``"connector.operation"`` strings.
    force_approval: tuple[str, ...] = ()
    #: Operations denied outright, as ``"connector.operation"`` strings.
    denied: tuple[str, ...] = ()
    #: Sensitivity assigned to a connector's outbound payloads. A connector that
    #: carries local-only data cannot transmit it to a remote destination.
    sensitivity: dict[str, DataSensitivity] = Field(default_factory=dict)

    def is_enabled(self, connector: str) -> bool:
        if connector in self.disabled:
            return False
        return not self.enabled or connector in self.enabled

    def is_denied(self, connector: str, operation: str) -> bool:
        return f"{connector}.{operation}" in self.denied

    def requires_approval(self, connector: str, operation: str, risk: ActionRisk) -> bool:
        if f"{connector}.{operation}" in self.force_approval:
            return True
        return risk.requires_approval

    def sensitivity_for(self, connector: str) -> DataSensitivity:
        return self.sensitivity.get(connector, DataSensitivity.INTERNAL)


__all__ = ["ConnectorPolicy"]
