"""Computer use & perception.

Backend-agnostic screen capture, structured perception (OCR/windows/accessibility
fusion), visual grounding, input control, policy, budgeting and the screen-data
boundary — everything the agent needs to *see* and *operate* a machine, kept
behind one interface so a new platform is a new backend and nothing more.

The agent reaches this package only through the computer tools
(``tools/builtin/computer.py``); it is executed by the existing tool executor, so
permissions, approvals, idempotency and audit are unchanged.
"""

from __future__ import annotations

from .base import CapturedImage, ComputerBackend, PointerButton, ScreenMetadata, WindowInfo
from .coords import CoordinateSpace, MonitorInfo, Point, Rect
from .grounding import GroundingKind, GroundingTarget, ResolvedTarget, resolve_target
from .observations import ObservationStore
from .perception import (
    ChangeDetector,
    NullAccessibility,
    Observation,
    PerceivedElement,
    PerceptionLayer,
    TextBlock,
)
from .policy import ActionBudget, ComputerPolicy
from .runtime import ComputerRuntime
from .screen_boundary import ScreenBoundaryDecision, ScreenDataBoundary, ScreenDataPolicy

__all__ = [
    "ActionBudget",
    "CapturedImage",
    "ChangeDetector",
    "ComputerBackend",
    "ComputerPolicy",
    "ComputerRuntime",
    "CoordinateSpace",
    "GroundingKind",
    "GroundingTarget",
    "MonitorInfo",
    "NullAccessibility",
    "Observation",
    "ObservationStore",
    "PerceivedElement",
    "PerceptionLayer",
    "Point",
    "PointerButton",
    "Rect",
    "ResolvedTarget",
    "ScreenBoundaryDecision",
    "ScreenDataBoundary",
    "ScreenDataPolicy",
    "ScreenMetadata",
    "TextBlock",
    "WindowInfo",
    "resolve_target",
]
