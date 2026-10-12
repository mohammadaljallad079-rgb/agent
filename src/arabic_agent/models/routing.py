"""Model routing.

The gateway must not simply "try the primary, then the fallbacks". Whether a
request may go to a *remote* model is a policy decision, and when policy
requires a local model the gateway must **fail closed** — refuse — rather than
silently sending private data to the cloud.

This module makes that decision explicit and auditable. It is a pure function of
its inputs (mode, boundary policy, privacy, capability, availability); it does
no probing and no heuristics. The gateway records the resulting
:class:`RoutingDecision` so every choice — including a refusal — is visible.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict

from ..core.enums import (
    DataSensitivity,
    ModelCapability,
    ModelLocation,
    OperatingMode,
)
from ..core.errors import PermissionDeniedError

#: Sensitivity at or above which a request is treated as local-only unless the
#: boundary policy explicitly permits remote processing.
_PRIVATE_SENSITIVITIES = frozenset({DataSensitivity.CONFIDENTIAL, DataSensitivity.SECRET})


class RoutingPolicy(BaseModel):
    """Operator-declared routing preferences."""

    model_config = ConfigDict(frozen=True)

    #: Declared operating mode (online / offline / hybrid).
    mode: OperatingMode = OperatingMode.ONLINE
    #: Whether the data boundary permits remote processing for this request.
    #: Derived from the :class:`~arabic_agent.local.boundary.DataBoundaryPolicy`.
    allow_remote: bool = True
    #: Whether a local model must be used regardless of mode (privacy mode).
    prefer_local: bool = False
    #: Whether falling back to a remote model is ever permitted.
    allow_remote_fallback: bool = True
    #: Hard local-only pin: no remote model may serve a request, and a remote
    #: fallback is never permitted, regardless of sensitivity or availability.
    #: Set by a local runtime in OFFLINE mode so "local only" is enforced in one
    #: place rather than re-derived (and potentially mis-derived) per call site.
    local_only: bool = False


class RoutingDecision(BaseModel):
    """The result of a routing evaluation, safe to log and audit."""

    model_config = ConfigDict(frozen=True)

    #: Models to try, in order.
    candidates: tuple[str, ...]
    #: Where the chosen candidate runs.
    location: ModelLocation
    #: Whether a remote model may serve this request at all.
    remote_allowed: bool
    #: Whether the decision was forced by a fail-closed policy.
    fail_closed: bool = False
    #: Human-readable explanation, recorded on the audit trail.
    reason: str = ""

    @property
    def is_empty(self) -> bool:
        return not self.candidates


class ModelRouter:
    """Turns a routing policy plus model inventories into an ordered plan.

    The router does not know *how* a model runs; it only needs to know which
    identifiers are local and which are remote, and which capabilities each
    offers.
    """

    def __init__(
        self,
        *,
        local_models: Sequence[str] = (),
        remote_models: Sequence[str] = (),
        capabilities: dict[str, frozenset[ModelCapability]] | None = None,
    ) -> None:
        self._local = tuple(local_models)
        self._remote = tuple(remote_models)
        self._capabilities = dict(capabilities or {})

    def _supports(self, model_id: str, capability: ModelCapability) -> bool:
        declared = self._capabilities.get(model_id)
        # Unknown capability set: assume the provider will reject if it cannot.
        return True if declared is None else capability in declared

    @property
    def local_models(self) -> tuple[str, ...]:
        """The model ids currently classified as local."""
        return self._local

    def add_local_model(self, model_id: str) -> None:
        """Classify ``model_id`` as local (idempotent).

        Used when a model is activated at runtime so offline / prefer-local
        routing can select it without rebuilding the router.
        """
        if model_id not in self._local:
            self._local = (*self._local, model_id)

    def add_remote_model(self, model_id: str) -> None:
        """Classify ``model_id`` as remote (idempotent)."""
        if model_id not in self._remote:
            self._remote = (*self._remote, model_id)

    def _local_candidates(self, capability: ModelCapability) -> list[str]:
        return [m for m in self._local if self._supports(m, capability)]

    def _remote_candidates(self, capability: ModelCapability) -> list[str]:
        return [m for m in self._remote if self._supports(m, capability)]

    def decide(
        self,
        *,
        policy: RoutingPolicy,
        capability: ModelCapability = ModelCapability.CHAT,
        sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
        requested_model: str | None = None,
    ) -> RoutingDecision:
        """Choose the ordered candidate list, or fail closed.

        Ordering:

        1. A request for a private payload, or a ``prefer_local`` policy, or
           ``OFFLINE`` mode, pins the request to local models.
        2. Otherwise local models are tried first (cheaper, private), then
           remote models if the boundary permits.
        3. If local is *required* and none is available, the decision is a
           fail-closed refusal — the caller must not proceed.
        """
        private = sensitivity in _PRIVATE_SENSITIVITIES
        # A hard local-only pin is stronger than mode/preference: it never
        # permits a remote candidate, even when a remote model would otherwise be
        # allowed and a local one is unavailable.
        local_required = (
            policy.local_only
            or policy.mode is OperatingMode.OFFLINE
            or policy.prefer_local
            or private
            or not policy.allow_remote
        )
        remote_permitted = (
            not policy.local_only
            and policy.allow_remote
            and policy.mode is not OperatingMode.OFFLINE
        )
        locals_available = self._local_candidates(capability)
        remotes_available = self._remote_candidates(capability) if remote_permitted else []

        if requested_model is not None:
            requested_is_local = requested_model in self._local
            if local_required and not requested_is_local:
                reason = (
                    f"policy requires a local model but {requested_model!r} is remote; "
                    "refusing to send data to the cloud"
                )
                if policy.local_only:
                    reason = (
                        "local-only mode is enforced; refusing to run a remote model "
                        f"{requested_model!r} instead of a local one"
                    )
                return RoutingDecision(
                    candidates=(),
                    location=ModelLocation.LOCAL,
                    remote_allowed=False,
                    fail_closed=True,
                    reason=reason,
                )
            location = ModelLocation.LOCAL if requested_is_local else ModelLocation.REMOTE
            return RoutingDecision(
                candidates=(requested_model,),
                location=location,
                remote_allowed=not local_required and remote_permitted,
                reason="explicit model requested",
            )

        if local_required:
            if not locals_available:
                reason = (
                    "local model required by policy but none is available; "
                    "failing closed instead of using a cloud model"
                )
                if policy.local_only:
                    reason = (
                        "local-only mode is enforced but no local model is available; "
                        "failing closed rather than falling back to a cloud model"
                    )
                return RoutingDecision(
                    candidates=(),
                    location=ModelLocation.LOCAL,
                    remote_allowed=False,
                    fail_closed=True,
                    reason=reason,
                )
            return RoutingDecision(
                candidates=tuple(locals_available),
                location=ModelLocation.LOCAL,
                remote_allowed=False,
                reason="local model required; routing locally",
            )

        ordered = [*locals_available, *remotes_available]
        if not ordered:
            return RoutingDecision(
                candidates=(),
                location=ModelLocation.REMOTE,
                remote_allowed=remote_permitted,
                fail_closed=True,
                reason="no model candidate satisfies the request",
            )
        # Fallback from local to remote is allowed only when the boundary
        # permits remote AND the policy has not forbidden remote fallback.
        if locals_available and remotes_available and not policy.allow_remote_fallback:
            ordered = locals_available
        location = ModelLocation.LOCAL if ordered[0] in self._local else ModelLocation.REMOTE
        return RoutingDecision(
            candidates=tuple(ordered),
            location=location,
            remote_allowed=remote_permitted,
            reason="local preferred; remote permitted by policy",
        )


def fail_closed_error(decision: RoutingDecision) -> PermissionDeniedError:
    """Build the error a fail-closed routing decision produces."""
    return PermissionDeniedError(
        decision.reason or "routing policy refused the request",
        details={
            "fail_closed": True,
            "location": decision.location.value,
        },
    )
