"""Screen data boundary: where may a screenshot be processed?

A screenshot can contain anything — passwords, private messages, medical
records — so it is treated as sensitive by default and its routing is a policy
decision, not an implementation detail. This module maps a screen-data policy
onto the existing :class:`~arabic_agent.local.boundary.DataBoundaryPolicy` and
:class:`~arabic_agent.core.enums.DataSensitivity`, and answers one question
deterministically: *may this observation be sent to a remote model?*

Policies:

* ``NEVER_STORE`` — the capture is used in-process and discarded immediately; it
  is never sent remotely and never retained.
* ``LOCAL_ONLY`` (the default) — the capture may be used locally (a local vision
  model, local OCR) but must never reach a remote model.
* ``CLOUD_ALLOWED`` — the operator has explicitly decided this screen's content
  may be processed by a remote multimodal model, subject to the existing
  boundary policy (offline mode still forbids it; ``SECRET`` never leaves).

The mapping fails closed: an unknown policy, or a remote destination under a
policy that does not permit it, is refused.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..core.enums import DataBoundary, DataSensitivity, OperatingMode
from ..local.boundary import BoundaryDecision, DataBoundaryPolicy


class ScreenDataPolicy(StrEnum):
    """How sensitive the content of a screen capture is considered."""

    NEVER_STORE = "never_store"
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


#: The boundary/sensitivity a policy maps to, for the *remote* destination.
#: A ``SECRET`` sensitivity can never leave the machine under any boundary
#: policy, which is what makes ``NEVER_STORE``/``LOCAL_ONLY`` fail closed.
_POLICY_SENSITIVITY: dict[ScreenDataPolicy, DataSensitivity] = {
    ScreenDataPolicy.NEVER_STORE: DataSensitivity.SECRET,
    ScreenDataPolicy.LOCAL_ONLY: DataSensitivity.CONFIDENTIAL,
    ScreenDataPolicy.CLOUD_ALLOWED: DataSensitivity.INTERNAL,
}

#: The data-boundary classification a policy implies for a *local* destination.
_POLICY_BOUNDARY: dict[ScreenDataPolicy, DataBoundary] = {
    ScreenDataPolicy.NEVER_STORE: DataBoundary.LOCAL,
    ScreenDataPolicy.LOCAL_ONLY: DataBoundary.LOCAL,
    ScreenDataPolicy.CLOUD_ALLOWED: DataBoundary.EITHER,
}


class ScreenBoundaryDecision(BaseModel):
    """The outcome of a screen-data boundary evaluation."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    policy: ScreenDataPolicy
    routed_to: DataBoundary
    reason: str
    may_persist: bool

    @property
    def may_go_remote(self) -> bool:
        return self.allowed and self.routed_to is DataBoundary.REMOTE


class ScreenDataBoundary:
    """Decides where a screen capture may be processed."""

    def __init__(
        self,
        *,
        policy: ScreenDataPolicy = ScreenDataPolicy.LOCAL_ONLY,
        mode: OperatingMode = OperatingMode.OFFLINE,
        allow_confidential_remote: bool = False,
    ) -> None:
        self._policy = policy
        self._boundary = DataBoundaryPolicy(
            mode, allow_confidential_remote=allow_confidential_remote
        )

    @property
    def policy(self) -> ScreenDataPolicy:
        return self._policy

    def set_mode(self, mode: OperatingMode) -> None:
        self._boundary.set_mode(mode)

    def evaluate(self, *, remote: bool) -> ScreenBoundaryDecision:
        """Answer whether a capture may be processed at ``remote`` or locally."""
        sensitivity = _POLICY_SENSITIVITY.get(self._policy, DataSensitivity.SECRET)
        boundary = _POLICY_BOUNDARY.get(self._policy, DataBoundary.LOCAL)
        if remote:
            decision: BoundaryDecision = self._boundary.evaluate(
                boundary=DataBoundary.REMOTE, sensitivity=sensitivity
            )
            # A CLOUD_ALLOWED policy still routes remotely only when the base
            # boundary permits it; otherwise the base decision (a refusal) wins.
            routed = DataBoundary.REMOTE if decision.allowed else DataBoundary.LOCAL
            return ScreenBoundaryDecision(
                allowed=decision.allowed and self._policy is ScreenDataPolicy.CLOUD_ALLOWED,
                policy=self._policy,
                routed_to=routed,
                reason=(
                    "screen content may be processed remotely"
                    if self._policy is ScreenDataPolicy.CLOUD_ALLOWED and decision.allowed
                    else f"screen data policy {self._policy.value} forbids remote processing: "
                    f"{decision.reason}"
                ),
                may_persist=self._policy is not ScreenDataPolicy.NEVER_STORE,
            )
        decision = self._boundary.evaluate(boundary=boundary, sensitivity=sensitivity)
        return ScreenBoundaryDecision(
            allowed=True,
            policy=self._policy,
            routed_to=DataBoundary.LOCAL,
            reason="local processing is always permitted for screen data",
            may_persist=self._policy is not ScreenDataPolicy.NEVER_STORE,
        )

    def may_send_remote(self) -> bool:
        return self.evaluate(remote=True).allowed


__all__ = ["ScreenBoundaryDecision", "ScreenDataBoundary", "ScreenDataPolicy"]
