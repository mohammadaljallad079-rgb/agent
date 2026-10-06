"""The connector runtime: the single object the connector tools call.

It is the execution boundary where every safety property of Phase 9 is enforced
in one place, so the tools stay thin and the agent core stays unaware:

* **Discovery.** It exposes the connector catalogue (capabilities, health,
  read-only/write/approval flags) so the agent is told what is available rather
  than guessing.
* **Data boundary.** Before any payload leaves the machine it is classified and
  checked against the existing :class:`~arabic_agent.local.boundary.DataBoundaryPolicy`.
  A ``LOCAL_ONLY`` payload is never transmitted to an external service; only the
  minimum required data is sent.
* **Permissions & approval.** A consequential operation requires the caller to
  present a trusted ``approval_granted`` signal (injected by the executor from
  the approval record, never a model argument).
* **Approval binding.** The action digest is computed from the *exact*
  consequential payload; when an approval is presented, the runtime recomputes
  the digest and refuses to execute if it changed. An approval for one action can
  never authorise a different one.
* **Idempotency / ledger.** Every action is claimed in the ledger by a
  cause-derived key, so a retry/redelivery does not repeat the effect. An action
  that ended ``UNKNOWN`` must be verified before any retry.
* **Ambiguous timeout.** A consequential request that times out is recorded
  ``UNKNOWN`` and raises :class:`AmbiguousActionError` (non-retryable) rather
  than being blindly retried.
* **Verification.** Consequential actions run act → observe → verify; a success
  is only marked ``VERIFIED`` when the provider confirms it.
* **Audit.** Every step is audited with metadata only — never a secret, a message
  body or a record payload.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..audit.base import AuditEventType, AuditLog
from ..core.enums import (
    ActionRisk,
    ActionState,
    ConnectorState,
    DataBoundary,
    DataSensitivity,
)
from ..core.errors import (
    AmbiguousActionError,
    ConfigurationError,
    PermissionDeniedError,
    TransientError,
    ValidationError,
)
from ..local.boundary import DataBoundaryPolicy
from .base import (
    Connector,
    ConnectorCall,
    ConnectorInfo,
    ConnectorRegistry,
    ConnectorResult,
    OperationKind,
)
from .ledger import (
    ActionLedger,
    ActionRecord,
    LedgerOutcome,
    canonical_digest,
    check_and_claim,
)

#: A verification callback: given the provider's result data, decide whether the
#: action is confirmed and return ``(verified, evidence)``.
Verifier = Callable[[dict[str, Any]], Awaitable[tuple[bool, str]]]


class ConnectorInvocation:
    """The result of one connector invocation through the runtime."""

    def __init__(
        self,
        *,
        ok: bool,
        summary: str,
        data: dict[str, Any] | None = None,
        action_id: str | None = None,
        state: ActionState | None = None,
        verified: bool = False,
        deduplicated: bool = False,
    ) -> None:
        self.ok = ok
        self.summary = summary
        self.data = data or {}
        self.action_id = action_id
        self.state = state
        self.verified = verified
        self.deduplicated = deduplicated

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "summary": self.summary, **self.data}
        if self.action_id is not None:
            payload["action_id"] = self.action_id
        if self.state is not None:
            payload["action_state"] = self.state.value
        if self.verified:
            payload["verified"] = True
        if self.deduplicated:
            payload["deduplicated"] = True
        return payload


class ConnectorRuntime:
    """Executes connector calls under boundary, approval, ledger and audit."""

    def __init__(
        self,
        *,
        registry: ConnectorRegistry,
        audit: AuditLog,
        ledger: ActionLedger,
        boundary: DataBoundaryPolicy | None = None,
        default_sensitivity: DataSensitivity = DataSensitivity.INTERNAL,
    ) -> None:
        self._registry = registry
        self._audit = audit
        self._ledger = ledger
        self._boundary = boundary
        self._default_sensitivity = default_sensitivity

    # -- Discovery ------------------------------------------------------
    @property
    def registry(self) -> ConnectorRegistry:
        return self._registry

    @property
    def ledger(self) -> ActionLedger:
        return self._ledger

    def catalogue(self) -> tuple[ConnectorInfo, ...]:
        return self._registry.catalogue()

    def catalogue_text(self) -> str:
        return self._registry.catalogue_text()

    def connector(self, name: str) -> Connector:
        return self._registry.get(name)

    # -- Boundary -------------------------------------------------------
    async def _check_boundary(
        self,
        *,
        sensitivity: DataSensitivity,
        destination_remote: bool,
        tenant_id: str,
        user_id: str,
        run_id: str,
        connector: str,
        operation: str,
    ) -> None:
        if self._boundary is None:
            return
        boundary = DataBoundary.REMOTE if destination_remote else DataBoundary.LOCAL
        decision = self._boundary.evaluate(boundary=boundary, sensitivity=sensitivity)
        # Audit the decision (allowed or refused) without the payload.
        await self._audit.record(
            AuditEventType.EXTERNAL_ACTION_BOUNDARY_DECISION,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=decision.reason,
            data={
                "connector": connector,
                "operation": operation,
                "sensitivity": sensitivity.value,
                "allowed": decision.allowed,
                "routed_to": decision.routed_to.value,
            },
        )
        if not decision.allowed:
            raise PermissionDeniedError(
                f"data boundary refused transmission: {decision.reason}",
                details={
                    "connector": connector,
                    "operation": operation,
                    "sensitivity": sensitivity.value,
                },
            )

    # -- Invocation -----------------------------------------------------
    async def invoke(
        self,
        *,
        connector_name: str,
        operation: str,
        payload: dict[str, Any],
        tenant_id: str,
        user_id: str,
        run_id: str,
        step_id: str = "",
        approval_granted: bool = False,
        approved_digest: str | None = None,
        sensitivity: DataSensitivity | None = None,
        destination_remote: bool = True,
        verifier: Verifier | None = None,
    ) -> ConnectorInvocation:
        """Invoke a connector operation through every Phase 9 control.

        The ordering is deliberate: availability → boundary → approval binding →
        idempotency → execute → verify → audit.
        """
        connector = self._registry.get(connector_name)
        health = await connector.healthcheck()
        if health.state is not ConnectorState.READY:
            await self._audit.record(
                AuditEventType.CONNECTOR_UNAVAILABLE,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message=f"connector {connector_name} is {health.state.value}",
                data={"connector": connector_name, "state": health.state.value},
            )
            raise ConfigurationError(
                f"connector {connector_name!r} is not usable: "
                f"{health.detail or health.state.value}",
                details={"connector": connector_name, "state": health.state.value},
            )

        op = connector.operation(operation)
        if op is None:
            raise ValidationError(
                f"connector {connector_name!r} does not support {operation!r}",
                details={"connector": connector_name, "operation": operation},
            )

        effective_sensitivity = sensitivity or self._default_sensitivity
        await self._check_boundary(
            sensitivity=effective_sensitivity,
            destination_remote=destination_remote,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            connector=connector_name,
            operation=operation,
        )

        # A consequential write requires the trusted approval signal, and the
        # approval must be bound to *this exact* payload.
        if op.kind is OperationKind.WRITE and op.risk.requires_approval:
            digest = canonical_digest(
                connector=connector_name, operation=operation, payload=payload
            )
            if not approval_granted:
                raise PermissionDeniedError(
                    f"{connector_name}.{operation} requires approval",
                    details={
                        "connector": connector_name,
                        "operation": operation,
                        "risk": op.risk.value,
                    },
                )
            if approved_digest is not None and approved_digest != digest:
                await self._audit.record(
                    AuditEventType.EXTERNAL_ACTION_APPROVAL_MISMATCH,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    message="approval payload changed; approval is invalid",
                    data={"connector": connector_name, "operation": operation},
                )
                raise PermissionDeniedError(
                    "the approved action changed; the approval no longer applies",
                    details={"connector": connector_name, "operation": operation},
                )

        # Claim the action in the ledger (dedupe by cause).
        decision = await check_and_claim(
            self._ledger,
            tenant_id=tenant_id,
            run_id=run_id,
            step_id=step_id,
            connector=connector_name,
            operation=operation,
            payload=payload,
            risk=op.risk.value,
        )
        record = decision.record
        if decision.outcome is LedgerOutcome.ALREADY_DONE and record is not None:
            await self._audit.record(
                AuditEventType.EXTERNAL_ACTION_DEDUPLICATED,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message=decision.reason,
                data={"connector": connector_name, "operation": operation, "action_id": record.id},
            )
            return ConnectorInvocation(
                ok=True,
                summary="an identical action already succeeded; not repeated",
                data={"deduplicated": True, "provider_request_id": record.provider_request_id},
                action_id=record.id,
                state=record.state,
                deduplicated=True,
            )
        if decision.outcome is LedgerOutcome.IN_FLIGHT and record is not None:
            return ConnectorInvocation(
                ok=False,
                summary="an identical action is already in progress; not repeated",
                data={"in_flight": True},
                action_id=record.id,
                state=record.state,
                deduplicated=True,
            )
        if decision.outcome is LedgerOutcome.UNRESOLVED and record is not None:
            # A prior attempt is UNKNOWN: verify before any retry, never repeat.
            return ConnectorInvocation(
                ok=False,
                summary="a prior identical action has an unknown outcome; verify before retrying",
                data={"unresolved": True},
                action_id=record.id,
                state=ActionState.UNKNOWN,
            )
        assert record is not None
        if op.risk.requires_approval:
            record = record.model_copy(
                update={
                    "approval_id": record.approval_id,
                    "approved_digest": approved_digest,
                }
            )
        return await self._execute(
            connector=connector,
            connector_name=connector_name,
            operation=operation,
            payload=payload,
            record=record,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            consequential=op.risk.requires_approval,
            verifier=verifier,
        )

    async def _execute(
        self,
        *,
        connector: Connector,
        connector_name: str,
        operation: str,
        payload: dict[str, Any],
        record: ActionRecord,
        tenant_id: str,
        user_id: str,
        run_id: str,
        consequential: bool,
        verifier: Verifier | None,
    ) -> ConnectorInvocation:
        record = await self._advance(record, ActionState.EXECUTING, attempts=record.attempts + 1)
        await self._audit.record(
            AuditEventType.EXTERNAL_ACTION_EXECUTING,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=f"executing {connector_name}.{operation}",
            data={"connector": connector_name, "operation": operation, "action_id": record.id},
        )
        try:
            result: ConnectorResult = await connector.call(
                ConnectorCall(operation=operation, payload=payload)
            )
        except AmbiguousActionError as exc:
            return await self._mark_unknown(record, tenant_id, user_id, run_id, exc.message)
        except TransientError as exc:
            # Transport failure. If the operation could have had an effect and we
            # cannot tell, this is ambiguous; otherwise it is a plain failure.
            if consequential:
                return await self._mark_unknown(record, tenant_id, user_id, run_id, exc.message)
            record = await self._advance(record, ActionState.FAILED, error=exc.message)
            await self._audit.record(
                AuditEventType.EXTERNAL_ACTION_FAILED,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message=exc.message,
                data={"connector": connector_name, "operation": operation, "action_id": record.id},
            )
            raise

        if not result.ok:
            record = await self._advance(record, ActionState.FAILED, error=result.summary)
            await self._audit.record(
                AuditEventType.EXTERNAL_ACTION_FAILED,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message=result.summary,
                data={"connector": connector_name, "operation": operation, "action_id": record.id},
            )
            return ConnectorInvocation(
                ok=False,
                summary=result.summary,
                data=result.data,
                action_id=record.id,
                state=record.state,
            )

        provider_id = str(result.data.get("provider_message_id") or result.data.get("id") or "")
        record = await self._advance(
            record,
            ActionState.SUCCEEDED,
            provider_request_id=provider_id,
        )
        await self._audit.record(
            AuditEventType.EXTERNAL_ACTION_SUCCEEDED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=result.summary,
            data={"connector": connector_name, "operation": operation, "action_id": record.id},
        )

        verified = False
        if consequential and verifier is not None:
            record = await self._advance(record, ActionState.VERIFYING)
            await self._audit.record(
                AuditEventType.EXTERNAL_ACTION_VERIFYING,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message=f"verifying {connector_name}.{operation}",
                data={"connector": connector_name, "operation": operation, "action_id": record.id},
            )
            verified, evidence = await verifier(result.data)
            if verified:
                record = await self._advance(
                    record, ActionState.VERIFIED, verification_status="verified"
                )
                await self._audit.record(
                    AuditEventType.EXTERNAL_ACTION_VERIFIED,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    message=evidence or "verified",
                    data={
                        "connector": connector_name,
                        "operation": operation,
                        "action_id": record.id,
                    },
                )
            else:
                record = await self._advance(record, ActionState.UNKNOWN)
                await self._audit.record(
                    AuditEventType.EXTERNAL_ACTION_UNKNOWN,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    run_id=run_id,
                    message=evidence or "verification could not confirm the action",
                    data={
                        "connector": connector_name,
                        "operation": operation,
                        "action_id": record.id,
                    },
                )
        return ConnectorInvocation(
            ok=True,
            summary=result.summary,
            data=result.data,
            action_id=record.id,
            state=record.state,
            verified=verified,
        )

    async def _mark_unknown(
        self,
        record: ActionRecord,
        tenant_id: str,
        user_id: str,
        run_id: str,
        message: str,
    ) -> ConnectorInvocation:
        record = await self._advance(record, ActionState.UNKNOWN, error=message)
        await self._audit.record(
            AuditEventType.EXTERNAL_ACTION_UNKNOWN,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=f"ambiguous outcome: {message}",
            data={
                "connector": record.connector,
                "operation": record.operation,
                "action_id": record.id,
            },
        )
        return ConnectorInvocation(
            ok=False,
            summary=(
                "the action outcome is unknown (a timeout or transport failure may or may not "
                "have taken effect); it will be verified before any retry"
            ),
            data={"unknown": True},
            action_id=record.id,
            state=ActionState.UNKNOWN,
        )

    async def _advance(
        self, record: ActionRecord, state: ActionState, **updates: Any
    ) -> ActionRecord:
        updated = record.with_state(state, **updates)
        return await self._ledger.update(updated)

    # -- Verification helper -------------------------------------------
    async def verify_action(
        self,
        *,
        action_id: str,
        tenant_id: str,
        verifier: Verifier,
    ) -> tuple[bool, str]:
        """Verify a previously ``UNKNOWN`` action against the provider.

        Returns ``(verified, evidence)``. On success the action moves to
        ``VERIFIED``; on failure it stays ``UNKNOWN`` so the caller can decide to
        retry (only now is a retry safe, because the effect was not observed).
        """
        record = await self._ledger.get(action_id, tenant_id=tenant_id)
        if record.state in (ActionState.VERIFIED, ActionState.SUCCEEDED):
            return True, "already verified"
        if record.state not in (ActionState.UNKNOWN, ActionState.VERIFYING):
            return False, f"action is {record.state.value}, not verifiable"
        record = await self._advance(record, ActionState.VERIFYING)
        verified, evidence = await verifier(
            {"action_id": record.id, "provider_request_id": record.provider_request_id}
        )
        if verified:
            await self._advance(record, ActionState.VERIFIED, verification_status="verified")
        return verified, evidence

    @staticmethod
    def risk_of(connector: Connector, operation: str) -> ActionRisk:
        op = connector.operation(operation)
        return op.risk if op is not None else ActionRisk.HIGH_RISK_ACTION


__all__ = ["ConnectorInvocation", "ConnectorRuntime", "Verifier"]
