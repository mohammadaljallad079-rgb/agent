"""An audio backend that is explicitly not available.

Used on platforms where no backend is implemented yet (macOS) and in tests that
need the *unavailable* path. It reports why it is unavailable so the speech tools
fail honestly instead of pretending to record or play.
"""

from __future__ import annotations

from collections.abc import Callable

from ...core.enums import PlatformKind
from ..base import AudioBackend, AudioClip, AudioDevice


class NullAudioBackend(AudioBackend):
    """An unavailable backend; every operation refuses with a clear reason."""

    name = "none"
    platform = PlatformKind.UNKNOWN
    available = False

    def __init__(self, reason: str = "no audio backend is implemented for this platform") -> None:
        self.unavailable_reason = reason

    async def list_devices(self) -> list[AudioDevice]:
        return []

    async def record(
        self,
        *,
        output_path: str,
        device_id: str = "",
        seconds: float = 5.0,
        sample_rate_hz: int = 16000,
        channels: int = 1,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AudioClip:
        self.require_available()
        raise AssertionError("unreachable")

    async def play(
        self,
        *,
        clip_path: str,
        device_id: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.require_available()
        raise AssertionError("unreachable")


__all__ = ["NullAudioBackend"]
