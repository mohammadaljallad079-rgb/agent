"""Local OCR.

OCR turns a screenshot into text with bounding boxes, which lets the agent
ground a target ("the button labelled X") without a vision model. It runs
entirely locally and offline.

The engine is a protocol, so a different OCR backend (a bundled model, a cloud
OCR service behind the boundary) can replace :class:`TesseractOcr` without
touching the perception layer. When no engine is available the perception layer
degrades honestly: :class:`NullOcr` reports ``available = False`` and returns no
text, rather than inventing it.

Tesseract is invoked through its CLI. This keeps the dependency optional and
avoids a hard Python binding; the ``ara`` and ``eng`` language packs are used
when present. Output is parsed from the TSV mode so every word carries a
bounding box and a confidence, and low-confidence text is marked as such — OCR
text is never presented as authoritative.
"""

from __future__ import annotations

import asyncio
import csv
import io
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from .capture import encode_png
from .coords import CoordinateSpace, Rect

if False:  # pragma: no cover
    from PIL.Image import Image

#: Words below this confidence are kept but flagged, never trusted blindly.
LOW_CONFIDENCE_THRESHOLD = 60.0


class OcrWord(BaseModel):
    """One recognised word with its box and confidence."""

    model_config = ConfigDict(frozen=True)

    text: str
    rect: Rect
    confidence: float = 0.0

    @property
    def low_confidence(self) -> bool:
        return self.confidence < LOW_CONFIDENCE_THRESHOLD


class OcrResult(BaseModel):
    """The result of recognising one image."""

    model_config = ConfigDict(frozen=True)

    words: tuple[OcrWord, ...] = ()
    languages: tuple[str, ...] = ()
    available: bool = True
    #: Set when OCR ran but produced nothing usable, with the reason.
    note: str = ""

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)


class OcrEngine(Protocol):
    """Recognise text in an image, locally."""

    name: str
    available: bool

    async def recognize(self, image: Image, *, region: Rect | None = None) -> OcrResult: ...


class NullOcr:
    """An OCR engine that is explicitly not available."""

    name = "none"
    available = False

    async def recognize(self, image: Image, *, region: Rect | None = None) -> OcrResult:
        return OcrResult(available=False, note="no local OCR engine is installed")


class TesseractOcr:
    """A real local OCR engine backed by the ``tesseract`` CLI."""

    name = "tesseract"
    #: Languages requested, in order. ``ara`` gives Arabic; ``eng`` Latin.
    languages: tuple[str, ...]

    def __init__(
        self,
        *,
        languages: tuple[str, ...] = ("ara", "eng"),
        binary: str | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.languages = tuple(languages)
        self._binary = binary or shutil.which("tesseract") or ""
        self._timeout = timeout_seconds
        self.available = bool(self._binary)

    async def recognize(self, image: Image, *, region: Rect | None = None) -> OcrResult:
        if not self.available:
            return OcrResult(available=False, note="tesseract binary not found on PATH")
        if region is not None:
            from .capture import crop

            image = crop(image, region)

        with tempfile.TemporaryDirectory(prefix="aap_ocr_") as tmp:
            image_path = Path(tmp) / "shot.png"
            image_path.write_bytes(encode_png(image))
            output_base = Path(tmp) / "out"
            argv = [
                self._binary,
                str(image_path),
                str(output_base),
                "-l",
                "+".join(self.languages),
                "--psm",
                "11",  # sparse text: best for arbitrary UI screenshots
                "tsv",
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, stderr = await asyncio.wait_for(process.communicate(), timeout=self._timeout)
            except (TimeoutError, OSError) as exc:
                return OcrResult(
                    available=True,
                    languages=self.languages,
                    note=f"ocr failed: {exc}",
                )
            tsv_path = output_base.with_suffix(".tsv")
            if process.returncode != 0 or not tsv_path.exists():
                note = stderr.decode("utf-8", errors="replace")[:200]
                return OcrResult(
                    available=True, languages=self.languages, note=f"ocr failed: {note}"
                )
            return self._parse_tsv(tsv_path.read_text(encoding="utf-8", errors="replace"), region)

    def _parse_tsv(self, content: str, region: Rect | None) -> OcrResult:
        words: list[OcrWord] = []
        reader = csv.DictReader(io.StringIO(content), delimiter="\t")
        offset_x = region.x if region is not None else 0
        offset_y = region.y if region is not None else 0
        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                confidence = float(row.get("conf") or -1)
                left = int(row.get("left") or 0)
                top = int(row.get("top") or 0)
                width = int(row.get("width") or 0)
                height = int(row.get("height") or 0)
            except (TypeError, ValueError):
                continue
            if confidence < 0 or width <= 0 or height <= 0:
                continue
            words.append(
                OcrWord(
                    text=text,
                    rect=Rect(
                        x=left + offset_x,
                        y=top + offset_y,
                        width=width,
                        height=height,
                        space=CoordinateSpace.LOGICAL,
                    ),
                    confidence=confidence,
                )
            )
        return OcrResult(words=tuple(words), languages=self.languages, available=True)


def build_ocr_engine(*, languages: tuple[str, ...] = ("ara", "eng")) -> OcrEngine:
    """Return a real OCR engine when one is available, else :class:`NullOcr`."""
    engine: OcrEngine = TesseractOcr(languages=languages)
    if engine.available:
        return engine
    return NullOcr()


__all__ = [
    "LOW_CONFIDENCE_THRESHOLD",
    "NullOcr",
    "OcrEngine",
    "OcrResult",
    "OcrWord",
    "TesseractOcr",
    "build_ocr_engine",
]
