"""Local runtime lifecycle state machine.

    STARTING → READY → RUNNING ⇄ PAUSED
                     ↘ STOPPING → STOPPED
    (any) → ERROR → STARTING (recover)

Transitions are enforced: an illegal transition raises
:class:`~arabic_agent.core.errors.ConflictError`, so the runtime can never
report ``READY`` before ``STARTING`` completed, accept work while ``STOPPING``,
or resume from ``STOPPED`` without a fresh start.
"""

from __future__ import annotations

from ..core.enums import RuntimeState
from ..core.errors import ConflictError

#: Allowed transitions from each state.
_TRANSITIONS: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.STARTING: frozenset(
        {RuntimeState.READY, RuntimeState.ERROR, RuntimeState.STOPPING}
    ),
    RuntimeState.READY: frozenset(
        {RuntimeState.RUNNING, RuntimeState.PAUSED, RuntimeState.STOPPING, RuntimeState.ERROR}
    ),
    RuntimeState.RUNNING: frozenset(
        {RuntimeState.READY, RuntimeState.PAUSED, RuntimeState.STOPPING, RuntimeState.ERROR}
    ),
    RuntimeState.PAUSED: frozenset(
        {RuntimeState.RUNNING, RuntimeState.READY, RuntimeState.STOPPING, RuntimeState.ERROR}
    ),
    RuntimeState.STOPPING: frozenset({RuntimeState.STOPPED, RuntimeState.ERROR}),
    RuntimeState.STOPPED: frozenset({RuntimeState.STARTING}),
    RuntimeState.ERROR: frozenset({RuntimeState.STARTING, RuntimeState.STOPPING}),
}


class LifecycleStateMachine:
    """Holds and enforces the runtime's lifecycle state."""

    def __init__(self, initial: RuntimeState = RuntimeState.STOPPED) -> None:
        self._state = initial

    @property
    def state(self) -> RuntimeState:
        return self._state

    def can_transition(self, target: RuntimeState) -> bool:
        return target in _TRANSITIONS[self._state]

    def transition(self, target: RuntimeState) -> RuntimeState:
        """Move to ``target``, returning the previous state.

        Raises :class:`ConflictError` on an illegal transition.
        """
        if not self.can_transition(target):
            raise ConflictError(
                f"illegal lifecycle transition: {self._state.value} → {target.value}",
                details={"from": self._state.value, "to": target.value},
            )
        previous = self._state
        self._state = target
        return previous

    @property
    def ready_for_work(self) -> bool:
        """Whether the runtime may accept work in its current state."""
        return self._state in (
            RuntimeState.READY,
            RuntimeState.RUNNING,
        )

    @property
    def accepts_tools(self) -> bool:
        """Whether tool execution is permitted (not paused/stopping/stopped)."""
        return self._state in (
            RuntimeState.READY,
            RuntimeState.RUNNING,
        )


#: Exported for tests that want to assert the transition table.
TRANSITIONS = _TRANSITIONS
