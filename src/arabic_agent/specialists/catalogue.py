"""Specialist catalogue and resolution.

Resolves a specialist id to a :class:`Specialist`, lists what is available, and
reports readiness — which of a specialist's required tools the running platform
actually registered. Readiness is honest: a specialist whose tools are partly
absent is reported ``degraded`` with the missing names, never silently presented
as complete.
"""

from __future__ import annotations

from typing import Any

from ..core.errors import NotFoundError
from .base import BUILTIN_SPECIALISTS, Specialist
from .builtin import BUILTIN, DEFAULT_SPECIALIST

__all__ = [
    "BUILTIN_SPECIALISTS",
    "DEFAULT_SPECIALIST",
    "Specialist",
    "SpecialistCatalogue",
    "default_catalogue",
]


class SpecialistCatalogue:
    """A lookup over specialist profiles."""

    def __init__(self, specialists: dict[str, Specialist] | None = None) -> None:
        self._specialists = dict(specialists or BUILTIN)

    def get(self, specialist_id: str | None) -> Specialist:
        """Resolve an id, defaulting to the general agent when ``None``/empty."""
        key = specialist_id or DEFAULT_SPECIALIST.id
        try:
            return self._specialists[key]
        except KeyError as exc:
            raise NotFoundError(
                f"unknown specialist: {key}",
                details={"known": sorted(self._specialists)},
            ) from exc

    def ids(self) -> list[str]:
        return sorted(self._specialists)

    def all(self) -> tuple[Specialist, ...]:
        return tuple(self._specialists[key] for key in self.ids())

    def readiness(self, available_tools: frozenset[str]) -> list[dict[str, Any]]:
        """Per-specialist readiness against the tools a runtime actually has.

        ``available`` — every tool the specialist advertises exists.
        ``degraded`` — some do; the missing names are listed.
        ``unavailable`` — none of a non-empty scope exists (or a required model
        capability is not configured, which the caller folds in separately).
        """
        report: list[dict[str, Any]] = []
        for specialist in self.all():
            wanted = specialist.advertised_tools()
            if not wanted:
                report.append(
                    {
                        "id": specialist.id,
                        "status": "available",
                        "missing_tools": [],
                        "tool_count": len(available_tools),
                    }
                )
                continue
            present = wanted & available_tools
            missing = sorted(wanted - available_tools)
            if not missing:
                status = "available"
            elif present:
                status = "degraded"
            else:
                status = "unavailable"
            report.append(
                {
                    "id": specialist.id,
                    "status": status,
                    "missing_tools": missing,
                    "tool_count": len(present),
                }
            )
        return report


def default_catalogue() -> SpecialistCatalogue:
    return SpecialistCatalogue()
