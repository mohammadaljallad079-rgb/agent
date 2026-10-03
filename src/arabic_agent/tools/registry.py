"""Tool registry.

Holds every available tool, produces the model-facing tool specs, and exposes
lookup by name. Registration is explicit and duplicate-safe so a misconfigured
plugin cannot silently shadow a built-in.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..core.errors import ConfigurationError, NotFoundError
from ..models.base import ToolSpec
from .base import Tool


class ToolRegistry:
    """A mutable registry of tools keyed by name."""

    def __init__(self, tools: Iterable[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools or ():
            self.register(tool)

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ConfigurationError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise NotFoundError(f"unknown tool: {name}", details={"known": self.names()}) from exc

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> tuple[Tool, ...]:
        return tuple(self._tools[name] for name in self.names())

    def specs(self) -> tuple[ToolSpec, ...]:
        """Model-facing specs for every registered tool."""
        return tuple(tool.spec() for tool in self.all())

    def subset(self, names: Iterable[str]) -> tuple[Tool, ...]:
        """Return the named tools, raising if any is unknown."""
        return tuple(self.get(name) for name in names)
