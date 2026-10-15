"""Speech policy and per-run budgeting.

The permission layer decides *whether* the agent may use the microphone or
speaker at all; this policy decides *how far* a single capture or synthesis may
go and how often it may repeat. It is a small, pure, declarative guard inside the
speech runtime, before any device or model call:

* a maximum recording length, so a runaway model cannot hold the microphone open;
* a per-run recording budget and a per-run synthesis budget, so voice cannot
  become a runaway of disk or API spend;
* a minimum interval between actions (anti-runaway);
* a maximum synthesis length in characters, so a runaway model cannot ask a TTS
  engine to read a book;
* a maximum audio-file size the runtime will accept for transcription/playback.

Every refusal is a classified :class:`PlatformError` the agent loop can recover
from, and is auditable.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ResourceExhaustedError, ValidationError


class SpeechPolicy(BaseModel):
    """Declarative bounds for microphone capture and speech synthesis."""

    model_config = ConfigDict(frozen=True)

    #: Longest single recording, in seconds.
    max_recording_seconds: float = Field(default=120.0, gt=0)
    #: Per-run cap on microphone recordings (anti-runaway).
    max_recordings_per_run: int = Field(default=20, ge=1)
    #: Per-run cap on synthesis calls (anti-runaway / cost).
    max_syntheses_per_run: int = Field(default=50, ge=1)
    #: Longest text that may be synthesized, in characters.
    max_synthesis_characters: int = Field(default=5000, ge=1)
    #: Largest audio file (bytes) accepted for transcription or playback.
    max_audio_bytes: int = Field(default=64 * 1024 * 1024, ge=1)
    #: Minimum seconds between two speech actions (anti-runaway).
    min_action_interval_seconds: float = Field(default=0.0, ge=0.0)
    #: Default language for capture and synthesis (Arabic-first).
    default_language: str = "ar"

    def check_recording_seconds(self, seconds: float) -> None:
        if seconds <= 0:
            raise ValidationError("recording duration must be positive")
        if seconds > self.max_recording_seconds:
            raise ResourceExhaustedError(
                "requested recording exceeds the maximum duration",
                details={"seconds": seconds, "max": self.max_recording_seconds},
            )

    def check_synthesis_text(self, text: str) -> None:
        if not text or not text.strip():
            raise ValidationError("there is no text to synthesize")
        if len(text) > self.max_synthesis_characters:
            raise ResourceExhaustedError(
                "text exceeds the maximum synthesis length",
                details={"characters": len(text), "max": self.max_synthesis_characters},
            )

    def check_audio_size(self, size_bytes: int) -> None:
        if size_bytes > self.max_audio_bytes:
            raise ResourceExhaustedError(
                "audio file exceeds the maximum accepted size",
                details={"size_bytes": size_bytes, "max": self.max_audio_bytes},
            )


class SpeechBudget:
    """Per-run counters for recordings and syntheses."""

    def __init__(self, policy: SpeechPolicy) -> None:
        self._policy = policy
        self._recordings = 0
        self._syntheses = 0
        self._last_at: float | None = None

    @property
    def recordings(self) -> int:
        return self._recordings

    @property
    def syntheses(self) -> int:
        return self._syntheses

    def _check_interval(self, now: float) -> None:
        if (
            self._last_at is not None
            and self._policy.min_action_interval_seconds > 0
            and now - self._last_at < self._policy.min_action_interval_seconds
        ):
            raise ResourceExhaustedError(
                "speech actions are happening too quickly; the minimum interval has not elapsed",
                details={"min_action_interval_seconds": self._policy.min_action_interval_seconds},
            )

    def consume_recording(self, *, now: float) -> None:
        if self._recordings >= self._policy.max_recordings_per_run:
            raise ResourceExhaustedError(
                "recording budget exhausted for this run",
                details={"max_recordings_per_run": self._policy.max_recordings_per_run},
            )
        self._check_interval(now)
        self._recordings += 1
        self._last_at = now

    def consume_synthesis(self, *, now: float) -> None:
        if self._syntheses >= self._policy.max_syntheses_per_run:
            raise ResourceExhaustedError(
                "synthesis budget exhausted for this run",
                details={"max_syntheses_per_run": self._policy.max_syntheses_per_run},
            )
        self._check_interval(now)
        self._syntheses += 1
        self._last_at = now


__all__ = ["SpeechBudget", "SpeechPolicy"]
