"""Recovery policy.

Maps a classified error to a bounded recovery action. The bounds (max retries,
max replans) are what prevent infinite loops: once exhausted, the policy aborts
rather than spinning forever.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.enums import ErrorClass
from .base import RecoveryAction

#: Which recovery action each error class defaults to.
_DEFAULT_ACTIONS: dict[ErrorClass, RecoveryAction] = {
    ErrorClass.TRANSIENT: RecoveryAction.RETRY,
    ErrorClass.TIMEOUT: RecoveryAction.RETRY,
    ErrorClass.VALIDATION: RecoveryAction.REPAIR,
    ErrorClass.PERMISSION: RecoveryAction.APPROVE,
    ErrorClass.NOT_FOUND: RecoveryAction.REPLAN,
    ErrorClass.CONFLICT: RecoveryAction.RETRY,
    ErrorClass.RESOURCE: RecoveryAction.ABORT,
    ErrorClass.EXTERNAL: RecoveryAction.RETRY,
    ErrorClass.CANCELLED: RecoveryAction.ABORT,
    ErrorClass.INTERNAL: RecoveryAction.ABORT,
}


@dataclass(frozen=True)
class RecoveryPolicy:
    """Bounded recovery strategy."""

    max_retries: int = 3
    max_replans: int = 3

    def decide(
        self,
        error_class: ErrorClass,
        *,
        step_attempts: int,
        replans_used: int,
    ) -> RecoveryAction:
        """Choose a recovery action, respecting the configured bounds."""
        action = _DEFAULT_ACTIONS.get(error_class, RecoveryAction.ABORT)

        if action is RecoveryAction.RETRY and step_attempts > self.max_retries:
            return RecoveryAction.ABORT
        if action is RecoveryAction.REPAIR and step_attempts > self.max_retries:
            return RecoveryAction.ABORT
        if action is RecoveryAction.REPLAN and replans_used >= self.max_replans:
            return RecoveryAction.ABORT
        return action
