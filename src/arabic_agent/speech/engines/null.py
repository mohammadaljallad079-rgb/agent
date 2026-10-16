"""A speech engine that is explicitly not available.

Used when no speech engine is installed (the common headless case) and in tests
that need the *unavailable* path. It reports why so the speech tools fail
honestly instead of pretending to transcribe or synthesize.
"""

from __future__ import annotations

from ..base import (
    SynthesisEngine,
    SynthesisResult,
    TranscriptionEngine,
    TranscriptionResult,
)


class NullTranscriptionEngine(TranscriptionEngine):
    """An unavailable speech-to-text engine."""

    name = "none"
    available = False

    def __init__(self, reason: str = "no speech-to-text engine is installed") -> None:
        self.unavailable_reason = reason

    async def transcribe(
        self, *, audio_path: str, language: str = "ar", prompt: str = ""
    ) -> TranscriptionResult:
        self.require_available()
        raise AssertionError("unreachable")


class NullSynthesisEngine(SynthesisEngine):
    """An unavailable text-to-speech engine."""

    name = "none"
    available = False

    def __init__(self, reason: str = "no text-to-speech engine is installed") -> None:
        self.unavailable_reason = reason

    async def synthesize(
        self, *, text: str, output_path: str, voice: str = "", language: str = "ar"
    ) -> SynthesisResult:
        self.require_available()
        raise AssertionError("unreachable")


__all__ = ["NullSynthesisEngine", "NullTranscriptionEngine"]
