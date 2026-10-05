"""Perception: turning a display into a structured observation.

The agent must reason from a *normalised* view of the screen, not from a pile of
unrelated raw outputs. :class:`PerceptionLayer` fuses the sources that are
actually available — a screenshot, window metadata and local OCR — into one
:class:`Observation` carrying text blocks, window rectangles, the active window
and a change hash. A structured accessibility source is supported through
:class:`AccessibilityProvider` when the platform offers one; on a platform
without it, the layer reports that honestly and does not pretend.

An observation is *not* the raw screenshot: the image is attached in memory for
the caller to send to a vision model or discard, and is never persisted or
audited. Every observation has an id and a change hash so the agent can tell
whether it is looking at fresh state before it acts.
"""

from __future__ import annotations

import time
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from ..core.ids import new_id
from .base import ComputerBackend, ScreenMetadata, WindowInfo
from .capture import bounded, decode_png, perceptual_hash
from .coords import MonitorInfo, Rect
from .ocr import NullOcr, OcrEngine, OcrResult

if False:  # pragma: no cover
    from PIL.Image import Image  # noqa: F401

#: An observation older than this is considered stale for a dangerous action.
DEFAULT_STALE_SECONDS = 30.0


class TextBlock(BaseModel):
    """A run of text found on screen, with provenance and confidence."""

    model_config = ConfigDict(frozen=True)

    text: str
    rect: Rect
    confidence: float = 0.0
    #: Where the text came from: ``"ocr"`` or ``"accessibility"``.
    source: str = "ocr"

    @property
    def low_confidence(self) -> bool:
        return self.source == "ocr" and self.confidence < 60.0


class PerceivedElement(BaseModel):
    """A candidate UI element the agent may target.

    Elements come from a structured accessibility tree when available; the
    fields mirror the common accessibility vocabulary so a target can be
    described semantically (role + label) instead of by raw coordinates.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    role: str = ""
    label: str = ""
    value: str = ""
    rect: Rect | None = None
    enabled: bool = True
    focused: bool = False
    selected: bool = False
    confidence: float = 1.0
    source: str = "accessibility"


class Observation(BaseModel):
    """A single, timestamped perception of the screen."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    id: str
    run_id: str
    captured_at: float
    metadata: ScreenMetadata
    monitor: MonitorInfo | None = None
    #: In-memory screenshot; excluded from serialisation and never persisted.
    image: object | None = Field(default=None, exclude=True, repr=False)
    image_width: int = 0
    image_height: int = 0
    change_hash: str = ""
    text_blocks: tuple[TextBlock, ...] = ()
    elements: tuple[PerceivedElement, ...] = ()
    windows: tuple[WindowInfo, ...] = ()
    active_window: WindowInfo | None = None
    #: Sources actually used, for honest reporting (e.g. ``("screenshot","ocr")``).
    sources: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(block.text for block in self.text_blocks)

    @property
    def age_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.captured_at)

    def is_stale(self, *, max_age: float = DEFAULT_STALE_SECONDS) -> bool:
        return self.age_seconds > max_age

    def summary(self) -> dict[str, object]:
        """A compact, serialisable summary safe for the model and the audit log.

        Deliberately excludes the raw image and any typed secrets: it reports
        geometry, the active window and recognised text only.
        """
        return {
            "observation_id": self.id,
            "captured_at": self.captured_at,
            "monitor": None if self.monitor is None else self.monitor.model_dump(),
            "image": {"width": self.image_width, "height": self.image_height},
            "active_window": None
            if self.active_window is None
            else self.active_window.model_dump(),
            "window_count": len(self.windows),
            "text_blocks": [
                {"text": b.text, "rect": b.rect.model_dump(), "confidence": b.confidence}
                for b in self.text_blocks[:200]
            ],
            "elements": [e.model_dump() for e in self.elements[:200]],
            "sources": list(self.sources),
            "notes": list(self.notes),
        }


class AccessibilityProvider(Protocol):
    """Structured UI metadata (role/label/value/state/rect) for a platform."""

    name: str
    available: bool

    async def elements(self) -> list[PerceivedElement]: ...


class NullAccessibility:
    """An accessibility source that is explicitly not available."""

    name = "none"
    available = False

    async def elements(self) -> list[PerceivedElement]:
        return []


class PerceptionLayer:
    """Fuses a backend and an OCR engine into observations."""

    def __init__(
        self,
        backend: ComputerBackend,
        *,
        ocr: OcrEngine | None = None,
        accessibility: AccessibilityProvider | None = None,
        max_dimension: int = 1600,
    ) -> None:
        self._backend = backend
        self._ocr: OcrEngine = ocr or NullOcr()
        self._accessibility: AccessibilityProvider = accessibility or NullAccessibility()
        self._max_dimension = max_dimension

    @property
    def ocr_available(self) -> bool:
        return getattr(self._ocr, "available", False)

    @property
    def accessibility_available(self) -> bool:
        return getattr(self._accessibility, "available", False)

    async def observe(
        self,
        *,
        run_id: str,
        monitor_id: int | None = None,
        include_ocr: bool = True,
        include_windows: bool = True,
        include_accessibility: bool = True,
    ) -> Observation:
        """Capture and structure the current screen.

        Only the sources requested *and actually available* are used; which ones
        ran is recorded in ``Observation.sources`` so a caller never mistakes a
        screenshot-only observation for one that also had OCR or accessibility.
        """
        metadata = await self._backend.get_screen_metadata()
        captured = await self._backend.capture_screen(monitor_id=monitor_id)
        image = decode_png(captured.data)
        image = bounded(image, max_dimension=self._max_dimension)
        monitor = None
        if monitor_id is not None:
            monitor = next((m for m in metadata.monitors if m.id == monitor_id), None)
        elif metadata.primary is not None:
            monitor = metadata.primary

        sources: list[str] = ["screenshot"]
        notes: list[str] = []
        text_blocks: list[TextBlock] = []

        if include_ocr and self.ocr_available:
            result: OcrResult = await self._ocr.recognize(image)
            sources.append("ocr")
            text_blocks.extend(
                TextBlock(
                    text=word.text,
                    rect=word.rect,
                    confidence=word.confidence,
                    source="ocr",
                )
                for word in result.words
            )
            if result.note:
                notes.append(result.note)
        elif include_ocr:
            notes.append("ocr unavailable")

        elements: list[PerceivedElement] = []
        if include_accessibility and self.accessibility_available:
            sources.append("accessibility")
            elements.extend(await self._accessibility.elements())
        elif include_accessibility:
            notes.append("accessibility tree unavailable on this platform")

        windows: list[WindowInfo] = []
        active: WindowInfo | None = None
        if include_windows and self._backend.supports_windows:
            windows = await self._backend.list_windows()
            active = next((w for w in windows if w.active), None)
            sources.append("windows")

        return Observation(
            id=new_id("obs"),
            run_id=run_id,
            captured_at=time.monotonic(),
            metadata=metadata,
            monitor=monitor,
            image=image,
            image_width=image.width,
            image_height=image.height,
            change_hash=perceptual_hash(image),
            text_blocks=tuple(text_blocks),
            elements=tuple(elements),
            windows=tuple(windows),
            active_window=active,
            sources=tuple(sources),
            notes=tuple(notes),
        )


class ChangeDetector:
    """Decides whether a new observation differs enough from the last one.

    Uses the perceptual hash so a cursor blink or a clock tick does not force a
    full re-analysis, but a real UI change does. The threshold is in hash bits;
    a small threshold (the default) is deliberately sensitive, because missing a
    real change is worse than re-analysing an unchanged screen.
    """

    def __init__(self, *, threshold: int = 3) -> None:
        from .capture import hamming_distance

        self._threshold = threshold
        self._distance = hamming_distance
        self._last_hash: str | None = None

    def update(self, observation: Observation) -> bool:
        """Record ``observation``; return whether the screen changed materially."""
        changed = (
            self._last_hash is None
            or self._distance(self._last_hash, observation.change_hash) > self._threshold
        )
        self._last_hash = observation.change_hash
        return changed

    def reset(self) -> None:
        self._last_hash = None


__all__ = [
    "DEFAULT_STALE_SECONDS",
    "AccessibilityProvider",
    "ChangeDetector",
    "NullAccessibility",
    "Observation",
    "PerceivedElement",
    "PerceptionLayer",
    "TextBlock",
]
