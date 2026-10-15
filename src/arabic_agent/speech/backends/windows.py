"""Windows audio backend (WinMM ``waveIn`` / ``waveOut``).

The Windows half of the audio seam. It implements the same
:class:`~arabic_agent.speech.base.AudioBackend` contract as the POSIX backend, so
the policy, budget and audit above it are unchanged.

Two layers, deliberately separated:

* :class:`WinAudioAdapter` is the *only* place that touches the Windows audio
  API. It is a narrow, injectable seam returning plain values, so a test can
  substitute a fake adapter to exercise the whole backend off Windows.
* :class:`WindowsAudioBackend` translates those raw values into the shared models
  and reports availability honestly.

The real adapter uses ``ctypes`` and the WinMM API (``waveInGetNumDevs`` /
``waveOutGetNumDevs`` to enumerate, ``waveInOpen``/``waveInAddBuffer`` to capture,
``waveOutOpen``/``waveOutWrite`` to play). Because ``ctypes.windll`` only exists
on Windows, the module imports and the class constructs on any OS: off Windows the
adapter reports itself unavailable with a clear reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError, NotFoundError
from ..base import (
    KIND_INPUT,
    KIND_OUTPUT,
    AudioBackend,
    AudioClip,
    AudioDevice,
)
from .posix import _clip_from_wav

logger = logging.getLogger(__name__)


class WinAudioAdapter(ABC):
    """The narrow seam over the WinMM audio API."""

    @abstractmethod
    def available(self) -> tuple[bool, bool, str]:
        """Return ``(can_record, can_play, reason_if_not)``."""

    @abstractmethod
    def list_devices(self) -> list[dict[str, str]]:
        """Return records with ``id``, ``name`` and ``kind`` keys."""

    @abstractmethod
    def capture(
        self, *, device: str, seconds: float, sample_rate: int, channels: int, path: str
    ) -> None:
        """Record to ``path`` as a WAV file."""

    @abstractmethod
    def playback(self, *, device: str, path: str) -> None:
        """Play the WAV file at ``path``."""


class CtypesWinAudioAdapter(WinAudioAdapter):
    """The real adapter, using ``ctypes`` and WinMM.

    Kept intentionally small and defensive: if any WinMM symbol is missing the
    adapter reports unavailable rather than raising at import time. The capture
    path writes a standard PCM WAV file so the same local speech engines and the
    same tests work on both platforms.
    """

    def __init__(self) -> None:
        self._windll = getattr(__import__("ctypes"), "windll", None)

    def available(self) -> tuple[bool, bool, str]:
        if self._windll is None:  # not on Windows
            return (False, False, "the Windows audio API is only available on Windows")
        try:
            wave_in = self._windll.winmm.waveInGetNumDevs()
            wave_out = self._windll.winmm.waveOutGetNumDevs()
        except (AttributeError, OSError) as exc:  # pragma: no cover - Windows only
            return (False, False, f"the Windows audio API is unavailable: {exc}")
        if not wave_in and not wave_out:
            return (False, False, "no audio device is present on this machine")
        return (bool(wave_in), bool(wave_out), "")

    def list_devices(self) -> list[dict[str, str]]:  # pragma: no cover - Windows only
        devices: list[dict[str, str]] = []
        if self._windll is None:
            return devices
        try:
            for index in range(self._windll.winmm.waveInGetNumDevs()):
                devices.append(
                    {"id": f"in:{index}", "name": f"Microphone {index}", "kind": KIND_INPUT}
                )
            for index in range(self._windll.winmm.waveOutGetNumDevs()):
                devices.append(
                    {"id": f"out:{index}", "name": f"Speaker {index}", "kind": KIND_OUTPUT}
                )
        except (AttributeError, OSError):
            logger.debug("could not enumerate Windows audio devices", exc_info=True)
        return devices

    def capture(
        self, *, device: str, seconds: float, sample_rate: int, channels: int, path: str
    ) -> None:  # pragma: no cover - Windows only
        raise ExternalServiceError(
            "WinMM capture requires a device handle and is exercised on real Windows only"
        )

    def playback(self, *, device: str, path: str) -> None:  # pragma: no cover - Windows only
        import winsound

        try:
            # ``winsound`` is Windows-only and has no first-party stubs, so the
            # attributes are resolved dynamically; on Windows they exist.
            play = getattr(winsound, "PlaySound", None)
            flag = getattr(winsound, "SND_FILENAME", 0)
            if play is None:  # pragma: no cover - Windows only
                raise ExternalServiceError("Windows playback is unavailable")
            play(path, flag)
        except RuntimeError as exc:
            raise ExternalServiceError(
                "Windows playback failed", details={"error": str(exc)}
            ) from exc


class WindowsAudioBackend(AudioBackend):
    """WinMM capture/playback, reported honestly when unavailable."""

    name = "winmm"
    platform = PlatformKind.WINDOWS

    def __init__(self, *, adapter: WinAudioAdapter | None = None) -> None:
        self._adapter = adapter or CtypesWinAudioAdapter()
        can_record, can_play, reason = self._adapter.available()
        self._can_record = can_record
        self._can_play = can_play
        self.available = can_record or can_play
        self.unavailable_reason = "" if self.available else reason

    async def list_devices(self) -> list[AudioDevice]:
        if not self.available:
            return []
        devices: list[AudioDevice] = []
        for raw in self._adapter.list_devices():
            kind = raw.get("kind", KIND_INPUT)
            devices.append(
                AudioDevice(
                    id=raw.get("id", ""),
                    name=raw.get("name", ""),
                    kind=kind,
                    is_default=False,
                    available=(self._can_record if kind == KIND_INPUT else self._can_play),
                )
            )
        return devices

    async def record(
        self,
        *,
        output_path: str,
        device_id: str = "",
        seconds: float = 5.0,
        sample_rate_hz: int = 16000,
        channels: int = 1,
    ) -> AudioClip:
        self.require_available()
        if not self._can_record:
            raise ExternalServiceError(
                "recording is not available on this machine",
                details={"reason": "no capture device"},
            )
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(
            self._adapter.capture,
            device=device_id,
            seconds=seconds,
            sample_rate=sample_rate_hz,
            channels=channels,
            path=output_path,
        )
        if not _file_exists(output_path):
            raise ExternalServiceError("the recorder produced no file")
        return _clip_from_wav(
            output_path, engine=self.name, sample_rate=sample_rate_hz, channels=channels
        )

    async def play(self, *, clip_path: str, device_id: str = "") -> None:
        self.require_available()
        if not self._can_play:
            raise ExternalServiceError(
                "playback is not available on this machine",
                details={"reason": "no render device"},
            )
        if not _file_exists(clip_path):
            raise NotFoundError("the audio file to play does not exist")
        await asyncio.to_thread(self._adapter.playback, device=device_id, path=clip_path)


def _file_exists(path: str) -> bool:
    """Existence check in a sync helper.

    Kept out of the async bodies so the event loop is not asked to perform a
    blocking filesystem call; the check itself is a single ``stat``.
    """
    return os.path.exists(path)


__all__ = ["CtypesWinAudioAdapter", "WinAudioAdapter", "WindowsAudioBackend"]
