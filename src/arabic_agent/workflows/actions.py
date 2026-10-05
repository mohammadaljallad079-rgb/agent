"""Deterministic workflow actions.

A deterministic step is *not* arbitrary code: it is a named, registered, pure
action. A workflow definition can only reference actions that exist in this
registry, so "deterministic code" in a workflow can never become an execution
hole. Actions are side-effect free and receive only the templated arguments;
anything with an external effect must be a *tool* step (which goes through the
policy gate) or an *agent* step.

The registry is deliberately small for now; integrations add actions here the
same way tools are registered.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

from ..core.errors import NotFoundError, ValidationError


class DeterministicAction(ABC):
    """A named, pure transformation used by deterministic steps."""

    name: str
    description: str = ""

    def __init__(self) -> None:
        if not getattr(self, "name", None):
            raise ValueError(f"{type(self).__name__} must define a name")

    @abstractmethod
    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """Return a JSON-serialisable result. Must be pure and side-effect free."""


class _FunctionAction(DeterministicAction):
    """Adapts a plain function into a :class:`DeterministicAction`."""

    def __init__(
        self, name: str, fn: Callable[[dict[str, Any]], dict[str, Any]], *, description: str = ""
    ) -> None:
        self.name = name
        self.description = description
        self._fn = fn
        super().__init__()

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return self._fn(arguments)


class _SetAction(DeterministicAction):
    name = "set"
    description = "Return the provided values unchanged (a pure 'assign' action)."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return dict(arguments)


class _MergeAction(DeterministicAction):
    name = "merge"
    description = "Merge the 'into' object with the 'with' object (shallow)."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        base = arguments.get("into", {})
        extra = arguments.get("with", {})
        if not isinstance(base, dict) or not isinstance(extra, dict):
            raise ValidationError("merge expects 'into' and 'with' objects")
        return {**base, **extra}


class _ConcatAction(DeterministicAction):
    name = "concat"
    description = "Concatenate a list of string parts into 'text'."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        parts = arguments.get("parts", [])
        if not isinstance(parts, list):
            raise ValidationError("concat expects a 'parts' list")
        separator = str(arguments.get("separator", ""))
        return {"text": separator.join("" if p is None else str(p) for p in parts)}


class _SumAction(DeterministicAction):
    name = "sum"
    description = "Sum a list of numbers, returned as 'total'."

    async def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        values = arguments.get("values", [])
        if not isinstance(values, list):
            raise ValidationError("sum expects a 'values' list")
        total: float | int = 0
        for value in values:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValidationError("sum expects numeric values")
            total += value
        return {"total": total}


class ActionRegistry:
    """Registry of deterministic actions, keyed by name."""

    def __init__(self, actions: list[DeterministicAction] | None = None) -> None:
        self._actions: dict[str, DeterministicAction] = {}
        for action in actions or []:
            self.register(action)

    def register(self, action: DeterministicAction, *, replace: bool = False) -> None:
        if action.name in self._actions and not replace:
            raise ValidationError(f"deterministic action already registered: {action.name}")
        self._actions[action.name] = action

    def get(self, name: str) -> DeterministicAction:
        try:
            return self._actions[name]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown deterministic action: {name}", details={"known": self.names()}
            ) from exc

    def has(self, name: str) -> bool:
        return name in self._actions

    def names(self) -> list[str]:
        return sorted(self._actions)


def default_actions() -> ActionRegistry:
    """The built-in, side-effect-free action set."""
    return ActionRegistry(
        [
            _SetAction(),
            _MergeAction(),
            _ConcatAction(),
            _SumAction(),
        ]
    )
