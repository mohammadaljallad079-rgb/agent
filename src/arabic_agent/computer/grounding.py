"""Visual grounding: turning perception into an actionable target.

An action needs a point. The agent should be able to express *what* it wants to
act on semantically — a text label it saw, an accessibility element, a bounding
box — and let this module resolve it to a concrete point in the observation's
coordinate space. Raw coordinates remain a valid low-level fallback.

Text matching is Arabic-aware: Arabic is right-to-left, may carry diacritics
(harakat) and tatweel, and uses presentation forms, so a naive substring match
would miss "الإعدادات" written with a different but equivalent form. Matching
normalises both sides first.
"""

from __future__ import annotations

import re
import unicodedata
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..core.errors import NotFoundError, ValidationError
from .coords import CoordinateSpace, Point, Rect
from .perception import Observation, PerceivedElement, TextBlock

#: Arabic diacritics (harakat) and tatweel, stripped before matching.
_ARABIC_DIACRITICS = re.compile(r"[\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06ed\u0640]")
#: Arabic presentation forms are normalised to their base letters via NFKC.
_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    """Normalise text for matching, Arabic-first.

    NFKC folds presentation forms; diacritics and tatweel are removed; case is
    folded; runs of whitespace collapse. The result is a stable key for
    comparing an agent-provided label against recognised on-screen text.
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = _ARABIC_DIACRITICS.sub("", folded)
    folded = folded.casefold()
    return _WHITESPACE.sub(" ", folded).strip()


class GroundingKind(StrEnum):
    """How a target was expressed."""

    POINT = "point"
    BBOX = "bbox"
    ELEMENT = "element"
    TEXT = "text"
    REGION = "region"


class GroundingTarget(BaseModel):
    """A description of what to act on, resolved against an observation."""

    model_config = ConfigDict(frozen=True)

    kind: GroundingKind
    #: For POINT / BBOX / REGION.
    point: Point | None = None
    rect: Rect | None = None
    #: For ELEMENT: an element id, or a role/label to match.
    element_id: str | None = None
    role: str | None = None
    #: For TEXT / ELEMENT label.
    text: str | None = None
    #: Whether a text match must be exact (normalised) or may be a substring.
    exact: bool = False
    space: CoordinateSpace = CoordinateSpace.LOGICAL

    @classmethod
    def at_point(
        cls, x: int, y: int, *, space: CoordinateSpace = CoordinateSpace.LOGICAL
    ) -> GroundingTarget:
        return cls(kind=GroundingKind.POINT, point=Point(x=x, y=y, space=space))

    @classmethod
    def at_rect(cls, rect: Rect) -> GroundingTarget:
        return cls(kind=GroundingKind.BBOX, rect=rect)

    @classmethod
    def by_text(cls, text: str, *, exact: bool = False) -> GroundingTarget:
        return cls(kind=GroundingKind.TEXT, text=text, exact=exact)

    @classmethod
    def by_element(
        cls, *, element_id: str | None = None, role: str | None = None, label: str | None = None
    ) -> GroundingTarget:
        return cls(kind=GroundingKind.ELEMENT, element_id=element_id, role=role, text=label)


class ResolvedTarget(BaseModel):
    """The concrete geometry an action will use."""

    model_config = ConfigDict(frozen=True)

    point: Point
    rect: Rect | None = None
    kind: GroundingKind
    #: What matched, for the audit trail (e.g. the recognised text).
    matched: str = ""
    confidence: float = 1.0
    source: str = ""


def resolve_target(target: GroundingTarget, observation: Observation) -> ResolvedTarget:
    """Resolve ``target`` to a concrete point using ``observation``.

    Raises :class:`NotFoundError` when a semantic target cannot be found (so the
    agent re-observes rather than clicking blindly) and :class:`ValidationError`
    for a malformed target.
    """
    if target.kind is GroundingKind.POINT:
        if target.point is None:
            raise ValidationError("point target requires a point")
        return ResolvedTarget(
            point=target.point, kind=target.kind, matched="point", source="coordinate"
        )

    if target.kind in (GroundingKind.BBOX, GroundingKind.REGION):
        if target.rect is None:
            raise ValidationError(f"{target.kind.value} target requires a rect")
        return ResolvedTarget(
            point=target.rect.center,
            rect=target.rect,
            kind=target.kind,
            matched="rect",
            source="coordinate",
        )

    if target.kind is GroundingKind.ELEMENT:
        element = _match_element(target, observation)
        if element is None or element.rect is None:
            raise NotFoundError(
                "no matching UI element in the current observation",
                details={
                    "role": target.role,
                    "label": target.text,
                    "element_id": target.element_id,
                },
            )
        return ResolvedTarget(
            point=element.rect.center,
            rect=element.rect,
            kind=target.kind,
            matched=element.label or element.id,
            confidence=element.confidence,
            source="accessibility",
        )

    if target.kind is GroundingKind.TEXT:
        if not target.text:
            raise ValidationError("text target requires text")
        block = _match_text(target.text, observation, exact=target.exact)
        if block is None:
            raise NotFoundError(
                "no on-screen text matched the target",
                details={"text": target.text},
            )
        return ResolvedTarget(
            point=block.rect.center,
            rect=block.rect,
            kind=target.kind,
            matched=block.text,
            confidence=block.confidence,
            source=block.source,
        )

    raise ValidationError(f"unsupported grounding kind: {target.kind}")


def _match_element(target: GroundingTarget, observation: Observation) -> PerceivedElement | None:
    for element in observation.elements:
        if target.element_id and element.id == target.element_id:
            return element
    if target.role or target.text:
        wanted_label = normalize_text(target.text) if target.text else ""
        wanted_role = normalize_text(target.role) if target.role else ""
        for element in observation.elements:
            if wanted_role and normalize_text(element.role) != wanted_role:
                continue
            if wanted_label and wanted_label not in normalize_text(element.label):
                continue
            return element
    return None


def _match_text(text: str, observation: Observation, *, exact: bool) -> TextBlock | None:
    wanted = normalize_text(text)
    if not wanted:
        return None
    best: TextBlock | None = None
    best_len = 0
    for block in observation.text_blocks:
        candidate = normalize_text(block.text)
        if not candidate:
            continue
        if exact and candidate == wanted:
            return block
        # Prefer the longest overlapping match, which is the most specific.
        if not exact and (wanted in candidate or candidate in wanted) and len(candidate) > best_len:
            best = block
            best_len = len(candidate)
    return best


__all__ = [
    "GroundingKind",
    "GroundingTarget",
    "ResolvedTarget",
    "normalize_text",
    "resolve_target",
]
