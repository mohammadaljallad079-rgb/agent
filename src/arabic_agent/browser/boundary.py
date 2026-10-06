"""Web-content data boundary: where may page content be processed?

A web page can contain anything -- a private dashboard, an account page, an
internal wiki -- so page content is treated as sensitive by default and its
routing is a policy decision, not an implementation detail. This module maps a
web-content policy onto the existing
:class:`~arabic_agent.local.boundary.DataBoundaryPolicy` and
:class:`~arabic_agent.core.enums.DataSensitivity`, and answers one question
deterministically: *may this page content be sent to a remote model?*

Policies:

* ``NEVER_STORE`` -- the content is used in-process and discarded immediately;
  it is never sent remotely and never retained.
* ``LOCAL_ONLY`` (the default) -- the content may be used locally (a local model,
  local extraction) but must never reach a remote model.
* ``CLOUD_ALLOWED`` -- the operator has explicitly decided this content may be
  processed by a remote model, subject to the existing boundary policy (offline
  mode still forbids it; ``SECRET`` never leaves).

The mapping fails closed: an unknown policy, or a remote destination under a
policy that does not permit it, is refused. This is what makes "a LOCAL_ONLY page
is never sent to a remote model" testable and enforceable rather than a hope.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..core.enums import DataBoundary, DataSensitivity, OperatingMode
from ..local.boundary import BoundaryDecision, DataBoundaryPolicy


class WebContentPolicy(StrEnum):
    """How sensitive the content of a web page is considered."""

    NEVER_STORE = "never_store"
    LOCAL_ONLY = "local_only"
    CLOUD_ALLOWED = "cloud_allowed"


#: Sensitivity a policy maps to for the *remote* destination. A ``SECRET``
#: sensitivity can never leave the machine under any boundary policy, which is
#: what makes ``NEVER_STORE``/``LOCAL_ONLY`` fail closed.
_POLICY_SENSITIVITY: dict[WebContentPolicy, DataSensitivity] = {
    WebContentPolicy.NEVER_STORE: DataSensitivity.SECRET,
    WebContentPolicy.LOCAL_ONLY: DataSensitivity.CONFIDENTIAL,
    WebContentPolicy.CLOUD_ALLOWED: DataSensitivity.INTERNAL,
}

#: The data-boundary classification a policy implies for a *local* destination.
_POLICY_BOUNDARY: dict[WebContentPolicy, DataBoundary] = {
    WebContentPolicy.NEVER_STORE: DataBoundary.LOCAL,
    WebContentPolicy.LOCAL_ONLY: DataBoundary.LOCAL,
    WebContentPolicy.CLOUD_ALLOWED: DataBoundary.EITHER,
}


class WebBoundaryDecision(BaseModel):
    """The outcome of a web-content boundary evaluation."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    policy: WebContentPolicy
    routed_to: DataBoundary
    reason: str
    may_persist: bool

    @property
    def may_go_remote(self) -> bool:
        return self.allowed and self.routed_to is DataBoundary.REMOTE


class WebDataBoundary:
    """Decides where page content may be processed."""

    def __init__(
        self,
        *,
        policy: WebContentPolicy = WebContentPolicy.LOCAL_ONLY,
        mode: OperatingMode = OperatingMode.OFFLINE,
        allow_confidential_remote: bool = False,
    ) -> None:
        self._policy = policy
        self._boundary = DataBoundaryPolicy(
            mode, allow_confidential_remote=allow_confidential_remote
        )

    @property
    def policy(self) -> WebContentPolicy:
        return self._policy

    def set_mode(self, mode: OperatingMode) -> None:
        self._boundary.set_mode(mode)

    def evaluate(self, *, remote: bool) -> WebBoundaryDecision:
        """Answer whether page content may be processed at ``remote`` or locally."""
        sensitivity = _POLICY_SENSITIVITY.get(self._policy, DataSensitivity.SECRET)
        boundary = _POLICY_BOUNDARY.get(self._policy, DataBoundary.LOCAL)
        if remote:
            decision: BoundaryDecision = self._boundary.evaluate(
                boundary=DataBoundary.REMOTE, sensitivity=sensitivity
            )
            permitted = decision.allowed and self._policy is WebContentPolicy.CLOUD_ALLOWED
            return WebBoundaryDecision(
                allowed=permitted,
                policy=self._policy,
                routed_to=DataBoundary.REMOTE if permitted else DataBoundary.LOCAL,
                reason=(
                    "web content may be processed remotely"
                    if permitted
                    else f"web content policy {self._policy.value} forbids remote processing: "
                    f"{decision.reason}"
                ),
                may_persist=self._policy is not WebContentPolicy.NEVER_STORE,
            )
        self._boundary.evaluate(boundary=boundary, sensitivity=sensitivity)
        return WebBoundaryDecision(
            allowed=True,
            policy=self._policy,
            routed_to=DataBoundary.LOCAL,
            reason="local processing is always permitted for web content",
            may_persist=self._policy is not WebContentPolicy.NEVER_STORE,
        )

    def may_send_remote(self) -> bool:
        return self.evaluate(remote=True).allowed


__all__ = ["WebBoundaryDecision", "WebContentPolicy", "WebDataBoundary"]
