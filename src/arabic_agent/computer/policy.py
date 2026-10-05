"""Computer-control policy and action budgeting.

The permission layer decides *whether* the agent may use a computer tool at all;
this policy decides *how far* a single action may go and how often it may
repeat. It is a small, pure, declarative guard that sits inside the computer
runtime, before any OS call:

* coordinate bounds (an out-of-screen click is clamped, never sent);
* allowed pointer buttons;
* a maximum scroll magnitude and typed-text length (no absurd payloads);
* an allow-list of keyboard keys/chords (no arbitrary key injection);
* per-run action budgets and a minimum interval between actions, so a model
  cannot turn computer use into a runaway clicker;
* a freshness requirement, so a dangerous action cannot be taken on a stale
  screenshot.

Every refusal is a classified :class:`PlatformError` the agent loop can recover
from, and is auditable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import PermissionDeniedError, ValidationError
from .base import PointerButton
from .coords import MonitorInfo, Point, clamp_point, is_on_screen

#: Keys and chords the keyboard tool may press, by normalised name. Deliberately
#: a fixed allow-list: the agent types text with ``type`` and uses named keys
#: only for navigation/confirmation, never arbitrary scan codes.
DEFAULT_ALLOWED_KEYS: frozenset[str] = frozenset(
    {
        "return",
        "enter",
        "tab",
        "escape",
        "esc",
        "backspace",
        "delete",
        "space",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
        "shift",
        "ctrl",
        "control",
        "alt",
        "meta",
        "super",
        "a",
        "c",
        "v",
        "x",
        "z",
        "s",
        "f",
        "f5",
        "f11",
    }
)


class ComputerPolicy(BaseModel):
    """Declarative bounds for computer use."""

    model_config = ConfigDict(frozen=True)

    #: Whether out-of-bounds points are clamped (True) or refused (False).
    clamp_out_of_bounds: bool = True
    #: Pointer buttons the agent may use.
    allowed_buttons: frozenset[PointerButton] = frozenset(
        {PointerButton.LEFT, PointerButton.RIGHT, PointerButton.MIDDLE}
    )
    #: Largest absolute scroll amount permitted in one action.
    max_scroll: int = Field(default=2000, ge=1)
    #: Largest number of characters permitted in one ``type`` action.
    max_text_length: int = Field(default=2000, ge=1)
    #: Named keys/chords the keyboard tool may press.
    allowed_keys: frozenset[str] = DEFAULT_ALLOWED_KEYS
    #: Per-run cap on pointer actions (move/click/scroll).
    max_pointer_actions: int = Field(default=60, ge=1)
    #: Per-run cap on keyboard actions.
    max_keyboard_actions: int = Field(default=60, ge=1)
    #: Per-run cap on screen captures.
    max_captures: int = Field(default=40, ge=1)
    #: Minimum seconds between two actions of the same kind (anti-runaway).
    min_action_interval_seconds: float = Field(default=0.0, ge=0.0)
    #: Whether an action must reference an observation no older than this.
    require_fresh_observation: bool = True
    max_observation_age_seconds: float = Field(default=30.0, gt=0)

    def normalize_point(self, monitors: list[MonitorInfo], point: Point) -> Point:
        """Validate/clamp a point against the monitor layout."""
        if is_on_screen(monitors, point):
            return point
        if self.clamp_out_of_bounds:
            return clamp_point(monitors, point)
        raise ValidationError(
            "target point is outside every monitor",
            details={"x": point.x, "y": point.y},
        )

    def check_button(self, button: PointerButton) -> None:
        if button not in self.allowed_buttons:
            raise PermissionDeniedError(
                "pointer button is not permitted", details={"button": button.value}
            )

    def check_scroll(self, amount: int) -> None:
        if amount == 0:
            raise ValidationError("scroll amount must be non-zero")
        if abs(amount) > self.max_scroll:
            raise ValidationError(
                "scroll amount exceeds the permitted maximum",
                details={"amount": amount, "max": self.max_scroll},
            )

    def check_text(self, text: str) -> None:
        if not text:
            raise ValidationError("text to type must be non-empty")
        if len(text) > self.max_text_length:
            raise ValidationError(
                "text exceeds the permitted length",
                details={"length": len(text), "max": self.max_text_length},
            )

    def check_keys(self, keys: list[str]) -> None:
        if not keys:
            raise ValidationError("at least one key is required")
        for key in keys:
            if key.strip().lower() not in self.allowed_keys:
                raise PermissionDeniedError(
                    "key is not on the permitted list", details={"key": key}
                )


class ActionBudget:
    """Counts actions within a run and enforces the per-kind caps.

    A single instance is created per run (or per tool context) so the caps are
    real, not advisory.
    """

    def __init__(self, policy: ComputerPolicy) -> None:
        self._policy = policy
        self._counts: dict[str, int] = {"pointer": 0, "keyboard": 0, "capture": 0}
        self._last_at: dict[str, float] = {}

    def _cap(self, kind: str) -> int:
        return {
            "pointer": self._policy.max_pointer_actions,
            "keyboard": self._policy.max_keyboard_actions,
            "capture": self._policy.max_captures,
        }[kind]

    def consume(self, kind: str, *, now: float) -> None:
        """Record one action of ``kind`` or refuse if the budget is exhausted."""
        if kind not in self._counts:
            raise ValidationError(f"unknown action kind: {kind}")
        interval = self._policy.min_action_interval_seconds
        if interval > 0:
            last = self._last_at.get(kind)
            if last is not None and now - last < interval:
                from ..core.errors import ResourceExhaustedError

                raise ResourceExhaustedError(
                    "computer action rate limit reached",
                    details={"kind": kind, "min_interval": interval},
                )
        if self._counts[kind] >= self._cap(kind):
            from ..core.errors import ResourceExhaustedError

            raise ResourceExhaustedError(
                f"per-run {kind} action budget exhausted",
                details={"kind": kind, "cap": self._cap(kind)},
            )
        self._counts[kind] += 1
        self._last_at[kind] = now

    def snapshot(self) -> dict[str, int]:
        return dict(self._counts)


__all__ = ["DEFAULT_ALLOWED_KEYS", "ActionBudget", "ComputerPolicy"]
