"""Specialist agents: one shared core, five declarative scopes.

See :mod:`arabic_agent.specialists.base` for why a specialist is data, not an
engine. Public surface:

* :data:`~arabic_agent.specialists.base.BUILTIN_SPECIALISTS` — the built-in ids.
* :class:`~arabic_agent.specialists.catalogue.SpecialistCatalogue` — lookup and
  readiness.
"""

from __future__ import annotations

from .base import (
    BUILTIN_SPECIALISTS,
    CODING,
    EDUCATION,
    GENERAL,
    VIDEO,
    VOICE,
    Specialist,
)
from .builtin import DEFAULT_SPECIALIST
from .catalogue import SpecialistCatalogue, default_catalogue

__all__ = [
    "BUILTIN_SPECIALISTS",
    "CODING",
    "DEFAULT_SPECIALIST",
    "EDUCATION",
    "GENERAL",
    "VIDEO",
    "VOICE",
    "Specialist",
    "SpecialistCatalogue",
    "default_catalogue",
]
