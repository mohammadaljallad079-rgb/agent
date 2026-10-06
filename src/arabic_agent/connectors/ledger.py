"""The external-action ledger, state machine and approval binding.

Every consequential external action (send an email, update a customer record,
POST to a webhook) is tracked as a durable :class:`ActionRecord` with a strict
:data:`ACTION_TRANSITIONS` state machine. The ledger is what makes retries,
worker restarts and ambiguous timeouts *safe*:

* **Approval binding.** An approval authorises one *exact* action. The action
  digest (a canonical hash of connector + operation + consequential payload) is
  computed when the approval is requested and stored on the ledger; the executor
  recomputes it at execution time and refuses to run when it changed. An
  approval for "send to A" can never authorise "send to B".
* **Idempotency.** An action carries an :attr:`ActionRecord.idempotency_key`
  derived from the *cause* (run + step + digest), so a retried step, a
  redelivered job or a duplicate trigger does not perform the effect twice. When
  a provider supports a native idempotency key it is sent too.
* **Ambiguous timeout.** A request that times out with no response is recorded
  ``UNKNOWN``, not ``FAILED``. The runtime then *verifies* against the provider
  before any retry — it never blindly repeats a consequential action.

The ledger stores only safe metadata and a canonical digest; it never stores the
raw payload (which may contain confidential data) or any secret.
"""

from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ActionOutcome, ActionState, StrEnum
from ..core.errors import ConflictError, NotFoundError
from ..core.ids import new_id
from ..core.redaction import redact

PREFIX_ACTION = "act"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ActionTransitionError(ConflictError):
    """An illegal transition was attempted on an action's state machine."""

    error_class = ConflictError.error_class
    retryable = False


#: Legal state transitions. The machine is deliberately small and total: every
#: reachable state has a defined set of successors, so an illegal transition is
#: a bug we can test for rather than an undefined behaviour.
ACTION_TRANSITIONS: dict[ActionState, frozenset[ActionState]] = {
    ActionState.PREPARED: frozenset({ActionState.AWAITING_APPROVAL, ActionState.EXECUTING}),
    ActionState.AWAITING_APPROVAL: frozenset(
        {ActionState.APPROVED, ActionState.CANCELLED, ActionState.FAILED}
    ),
    ActionState.APPROVED: frozenset({ActionState.EXECUTING, ActionState.CANCELLED}),
    ActionState.EXECUTING: frozenset(
        {
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.UNKNOWN,
            ActionState.CANCELLED,
        }
    ),
    ActionState.UNKNOWN: frozenset(
        {ActionState.VERIFYING, ActionState.SUCCEEDED, ActionState.FAILED, ActionState.CANCELLED}
    ),
    ActionState.VERIFYING: frozenset(
        {
            ActionState.VERIFIED,
            ActionState.SUCCEEDED,
            ActionState.FAILED,
            ActionState.UNKNOWN,
        }
    ),
    ActionState.SUCCEEDED: frozenset({ActionState.VERIFYING, ActionState.VERIFIED}),
    ActionState.VERIFIED: frozenset(),
    ActionState.FAILED: frozenset({ActionState.EXECUTING}),
    ActionState.CANCELLED: frozenset(),
}

#: States after which no further external effect may be performed.
TERMINAL_STATES = frozenset({ActionState.VERIFIED, ActionState.CANCELLED})

#: States from which a retry (a fresh EXECUTING attempt) is legitimate.
RETRYABLE_STATES = frozenset({ActionState.FAILED})


def can_transition(current: ActionState, target: ActionState) -> bool:
    return target in ACTION_TRANSITIONS.get(current, frozenset())


def canonical_digest(*, connector: str, operation: str, payload: dict[str, object]) -> str:
    """A stable digest of an action's *consequential* identity.

    The digest is what an approval is bound to. It is computed over the
    connector, the operation and a redacted, canonically-serialised payload, so
    two actions that would have the same external effect share a digest and any
    change to recipient/payload/attachment changes it.
    """
    canonical = json.dumps(
        {"connector": connector, "operation": operation, "payload": redact(payload)},
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ActionRecord(BaseModel):
    """Durable, safe metadata about one external action attempt."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_ACTION))
    tenant_id: str
    run_id: str = ""
    step_id: str = ""
    connector: str
    operation: str
    risk: str = ""
    state: ActionState = ActionState.PREPARED
    #: Canonical digest of the consequential payload (never the payload itself).
    request_digest: str
    #: Cause-derived key that makes the effect idempotent across retries.
    idempotency_key: str = ""
    #: The approval this action is bound to, if any.
    approval_id: str | None = None
    #: The digest the approval was granted for (binding check).
    approved_digest: str | None = None
    attempts: int = 0
    #: The provider's own identifier for the effect (message id, record id).
    provider_request_id: str = ""
    verification_status: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def with_state(self, state: ActionState, **updates: object) -> ActionRecord:
        if not can_transition(self.state, state):
            raise ActionTransitionError(
                f"illegal action transition {self.state.value} -> {state.value}",
                details={"action_id": self.id, "from": self.state.value, "to": state.value},
            )
        return self.model_copy(update={"state": state, "updated_at": _utcnow(), **updates})


class ActionLedger(ABC):
    """Persistence contract for the external-action ledger."""

    @abstractmethod
    async def add(self, record: ActionRecord) -> ActionRecord: ...

    @abstractmethod
    async def get(self, action_id: str, *, tenant_id: str) -> ActionRecord: ...

    @abstractmethod
    async def update(self, record: ActionRecord) -> ActionRecord: ...

    @abstractmethod
    async def find_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ActionRecord | None: ...

    @abstractmethod
    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[ActionRecord]: ...

    async def aclose(self) -> None:
        return None


class InMemoryActionLedger(ActionLedger):
    """Process-local ledger for the local runtime and tests."""

    def __init__(self) -> None:
        self._items: dict[str, ActionRecord] = {}
        self._by_key: dict[tuple[str, str], str] = {}

    async def add(self, record: ActionRecord) -> ActionRecord:
        self._items[record.id] = record
        if record.idempotency_key:
            self._by_key[(record.tenant_id, record.idempotency_key)] = record.id
        return record

    async def get(self, action_id: str, *, tenant_id: str) -> ActionRecord:
        record = self._items.get(action_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFoundError("action not found", details={"action_id": action_id})
        return record

    async def update(self, record: ActionRecord) -> ActionRecord:
        if record.id not in self._items:
            raise NotFoundError("action not found", details={"action_id": record.id})
        self._items[record.id] = record
        if record.idempotency_key:
            self._by_key[(record.tenant_id, record.idempotency_key)] = record.id
        return record

    async def find_by_idempotency(
        self, *, tenant_id: str, idempotency_key: str
    ) -> ActionRecord | None:
        action_id = self._by_key.get((tenant_id, idempotency_key))
        if action_id is None:
            return None
        return self._items.get(action_id)

    async def list_for_run(self, *, tenant_id: str, run_id: str) -> list[ActionRecord]:
        return [r for r in self._items.values() if r.tenant_id == tenant_id and r.run_id == run_id]


class LedgerOutcome(StrEnum):
    """What the ledger decided about a prospective action."""

    PROCEED = "proceed"  # no prior action; perform the effect
    ALREADY_DONE = "already_done"  # a prior identical action succeeded; do not repeat
    IN_FLIGHT = "in_flight"  # a prior identical action is executing/awaiting; do not repeat
    UNRESOLVED = "unresolved"  # a prior identical action is UNKNOWN; verify before retrying


class LedgerDecision(BaseModel):
    """The result of asking the ledger whether an action may proceed."""

    model_config = ConfigDict(frozen=True)

    outcome: LedgerOutcome
    record: ActionRecord | None = None
    reason: str = ""


async def check_and_claim(
    ledger: ActionLedger,
    *,
    tenant_id: str,
    run_id: str,
    step_id: str,
    connector: str,
    operation: str,
    payload: dict[str, object],
    risk: str = "",
) -> LedgerDecision:
    """Decide whether an action may proceed, deduplicating by cause.

    Returns ``ALREADY_DONE`` when an identical action already verified/succeeded
    (so the effect is not repeated), ``IN_FLIGHT`` when one is already executing
    or awaiting approval, and ``UNRESOLVED`` when a prior attempt ended
    ``UNKNOWN`` and must be verified before any retry. Only ``PROCEED`` (or the
    explicit retry of a ``FAILED`` action) performs the effect.
    """
    digest = canonical_digest(connector=connector, operation=operation, payload=payload)
    key = f"{run_id}:{step_id}:{digest}"
    existing = await ledger.find_by_idempotency(tenant_id=tenant_id, idempotency_key=key)
    if existing is not None:
        if existing.state in (ActionState.VERIFIED, ActionState.SUCCEEDED):
            return LedgerDecision(
                outcome=LedgerOutcome.ALREADY_DONE,
                record=existing,
                reason="an identical action already succeeded; not repeating it",
            )
        if existing.state in (
            ActionState.EXECUTING,
            ActionState.APPROVED,
            ActionState.AWAITING_APPROVAL,
            ActionState.VERIFYING,
        ):
            return LedgerDecision(
                outcome=LedgerOutcome.IN_FLIGHT,
                record=existing,
                reason="an identical action is already in progress; not repeating it",
            )
        if existing.state is ActionState.UNKNOWN:
            return LedgerDecision(
                outcome=LedgerOutcome.UNRESOLVED,
                record=existing,
                reason="a prior attempt has an unknown outcome; verify before retrying",
            )
        # FAILED (or CANCELLED): a retry is legitimate. Reuse the record.
        return LedgerDecision(outcome=LedgerOutcome.PROCEED, record=existing, reason="retry")
    record = ActionRecord(
        tenant_id=tenant_id,
        run_id=run_id,
        step_id=step_id,
        connector=connector,
        operation=operation,
        risk=risk,
        request_digest=digest,
        idempotency_key=key,
    )
    await ledger.add(record)
    return LedgerDecision(outcome=LedgerOutcome.PROCEED, record=record, reason="first attempt")


def outcome_for_state(state: ActionState) -> ActionOutcome:
    """Map an action state onto the recovery-relevant outcome."""
    if state in (ActionState.VERIFIED, ActionState.SUCCEEDED):
        return ActionOutcome.SUCCESS
    if state is ActionState.UNKNOWN:
        return ActionOutcome.UNKNOWN
    if state is ActionState.FAILED:
        return ActionOutcome.FAILURE
    if state is ActionState.VERIFYING:
        return ActionOutcome.NEEDS_VERIFICATION
    if state is ActionState.AWAITING_APPROVAL:
        return ActionOutcome.NEEDS_USER
    return ActionOutcome.FAILURE


__all__ = [
    "ACTION_TRANSITIONS",
    "RETRYABLE_STATES",
    "TERMINAL_STATES",
    "ActionLedger",
    "ActionRecord",
    "ActionTransitionError",
    "InMemoryActionLedger",
    "LedgerDecision",
    "LedgerOutcome",
    "can_transition",
    "canonical_digest",
    "check_and_claim",
    "outcome_for_state",
]
