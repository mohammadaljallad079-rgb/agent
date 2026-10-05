"""Data boundary: may this data leave the machine?

The single question the boundary answers, deterministically, is: *given the
current operating mode, the sensitivity of the data, and the destination of an
operation, is it permitted?* It is deliberately a pure function of its inputs —
there is no network probe, no heuristic, no "try it and see". That is what makes
"can this data leave this machine?" auditable and testable.

The decision is a :class:`BoundaryDecision`; it is recorded on the audit log by
the caller so every cross-boundary attempt (allowed or refused) is visible.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..core.enums import DataBoundary, DataSensitivity, OperatingMode


class BoundaryDecision(BaseModel):
    """Outcome of a data-boundary evaluation."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    #: Where the operation was routed to (``local`` or ``remote``).
    routed_to: DataBoundary
    #: The sensitivity that drove the decision.
    sensitivity: DataSensitivity
    mode: OperatingMode

    @classmethod
    def allow(
        cls,
        *,
        routed_to: DataBoundary,
        sensitivity: DataSensitivity,
        mode: OperatingMode,
        reason: str,
    ) -> BoundaryDecision:
        return cls(
            allowed=True,
            reason=reason,
            routed_to=routed_to,
            sensitivity=sensitivity,
            mode=mode,
        )

    @classmethod
    def deny(
        cls,
        *,
        routed_to: DataBoundary,
        sensitivity: DataSensitivity,
        mode: OperatingMode,
        reason: str,
    ) -> BoundaryDecision:
        return cls(
            allowed=False,
            reason=reason,
            routed_to=routed_to,
            sensitivity=sensitivity,
            mode=mode,
        )


#: Which sensitivities may be sent to a remote destination at all, regardless of
#: mode. Secrets never leave the machine; this is not configurable.
_NEVER_REMOTE = frozenset({DataSensitivity.SECRET})
#: Sensitivities that require an explicit policy opt-in to go remote.
_OPT_IN_REMOTE = frozenset({DataSensitivity.CONFIDENTIAL})


class DataBoundaryPolicy:
    """Decides whether a payload may be processed locally or remotely.

    Rules, in order:

    1. A ``SECRET`` payload may never be sent remotely, in any mode.
    2. In ``OFFLINE`` mode, nothing may be sent remotely.
    3. A ``LOCAL``-boundary operation always routes locally.
    4. A ``CONFIDENTIAL`` payload is sent remotely only if the policy was
       explicitly configured to allow it (``allow_confidential_remote``).
    5. Otherwise the payload may be sent remotely.
    """

    def __init__(
        self,
        mode: OperatingMode,
        *,
        allow_confidential_remote: bool = False,
    ) -> None:
        self._mode = mode
        self._allow_confidential_remote = allow_confidential_remote

    @property
    def mode(self) -> OperatingMode:
        return self._mode

    def set_mode(self, mode: OperatingMode) -> None:
        self._mode = mode

    def evaluate(
        self,
        *,
        boundary: DataBoundary,
        sensitivity: DataSensitivity,
    ) -> BoundaryDecision:
        """Answer whether the operation may proceed and where it routes."""
        if boundary is DataBoundary.LOCAL:
            return BoundaryDecision.allow(
                routed_to=DataBoundary.LOCAL,
                sensitivity=sensitivity,
                mode=self._mode,
                reason="operation is local-bound; it stays on this machine",
            )

        remote_wanted = boundary is DataBoundary.REMOTE
        # EITHER: prefer local when offline, else remote is acceptable.
        if not remote_wanted and self._mode is OperatingMode.OFFLINE:
            return BoundaryDecision.allow(
                routed_to=DataBoundary.LOCAL,
                sensitivity=sensitivity,
                mode=self._mode,
                reason="offline mode; routing locally",
            )

        if sensitivity in _NEVER_REMOTE:
            return BoundaryDecision.deny(
                routed_to=DataBoundary.REMOTE,
                sensitivity=sensitivity,
                mode=self._mode,
                reason="secret data must never leave the machine",
            )

        if self._mode is OperatingMode.OFFLINE:
            return BoundaryDecision.deny(
                routed_to=DataBoundary.REMOTE,
                sensitivity=sensitivity,
                mode=self._mode,
                reason="offline mode forbids remote processing",
            )

        if sensitivity in _OPT_IN_REMOTE and not self._allow_confidential_remote:
            return BoundaryDecision.deny(
                routed_to=DataBoundary.REMOTE,
                sensitivity=sensitivity,
                mode=self._mode,
                reason="confidential data may only go remote with explicit opt-in",
            )

        return BoundaryDecision.allow(
            routed_to=DataBoundary.REMOTE,
            sensitivity=sensitivity,
            mode=self._mode,
            reason="remote processing permitted by boundary policy",
        )

    def may_leave_machine(self, sensitivity: DataSensitivity) -> bool:
        """Convenience predicate for a plain sensitivity check."""
        return self.evaluate(boundary=DataBoundary.REMOTE, sensitivity=sensitivity).allowed
