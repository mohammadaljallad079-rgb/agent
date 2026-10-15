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

The real adapter uses ``ctypes`` and the WinMM API:

* ``waveInGetNumDevs`` / ``waveInGetDevCapsW`` to enumerate capture devices and
  ``waveOutGetNumDevs`` / ``waveOutGetDevCapsW`` for render devices, so device
  *names* come from the OS rather than a synthetic ``Microphone 0``;
* ``waveInOpen`` → ``waveInPrepareHeader`` → ``waveInAddBuffer`` → ``waveInStart``
  to capture, polling the buffer's ``WHDR_DONE`` flag, then writing a standard
  PCM WAV file;
* ``waveOutOpen`` → ``waveOutPrepareHeader`` → ``waveOutWrite`` to play, polling
  ``WHDR_DONE`` and honouring the requested render device.

The device id is the string the backend hands out (``in:0`` / ``out:1``); the
empty string selects the system default mapper (``WAVE_MAPPER``). Because
``ctypes.windll`` only exists on Windows, the module imports and the class
constructs on any OS: off Windows the adapter reports itself unavailable with a
clear reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path
from typing import Any

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

#: WinMM ``WAVE_MAPPER`` — "use the system default device".
WAVE_MAPPER = 0xFFFFFFFF
#: ``WAVE_FORMAT_PCM``.
WAVE_FORMAT_PCM = 1
#: ``WAVEHDR.dwFlags`` bits.
WHDR_DONE = 0x00000001
#: ``waveInOpen``/``waveOutOpen`` callback flag for no callback.
CALLBACK_NULL = 0x00000000
#: How long to wait for a capture/playback buffer to complete beyond its nominal
#: duration before giving up (seconds).
_COMPLETION_GRACE = 5.0


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
        self,
        *,
        device: str,
        seconds: float,
        sample_rate: int,
        channels: int,
        path: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Record to ``path`` as a WAV file."""

    @abstractmethod
    def playback(
        self, *, device: str, path: str, should_cancel: Callable[[], bool] | None = None
    ) -> None:
        """Play the WAV file at ``path``."""


def _device_index(device_id: str, *, default: int = -1) -> int:
    """Parse a backend device id (``in:2``) into a WinMM index.

    The empty string and the ``default`` token select the system mapper.
    """
    if not device_id or device_id == "default":
        return default
    _, _, raw = device_id.partition(":")
    try:
        return int(raw)
    except ValueError:
        return default


class CtypesWinAudioAdapter(WinAudioAdapter):
    """The real adapter, using ``ctypes`` and WinMM.

    Kept defensive: if any WinMM symbol is missing the adapter reports
    unavailable rather than raising at import time. The capture and playback
    paths write and read standard PCM WAV files so the same local speech engines
    and the same tests work on both platforms.
    """

    def __init__(self) -> None:
        import ctypes

        self._ctypes = ctypes
        self._windll = getattr(ctypes, "windll", None)
        self._winmm: Any = (
            getattr(self._windll, "winmm", None) if self._windll is not None else None
        )
        self._configured = False

    # -- Setup -----------------------------------------------------------
    def _configure(self) -> None:
        """Declare argtypes/restypes once, so 64-bit handles are not truncated."""
        if self._configured or self._winmm is None:
            return
        import ctypes
        from ctypes import wintypes

        winmm = self._winmm
        winmm.waveInGetNumDevs.restype = wintypes.UINT
        winmm.waveOutGetNumDevs.restype = wintypes.UINT
        for name in ("waveInOpen", "waveOutOpen"):
            fn = getattr(winmm, name)
            fn.argtypes = [
                ctypes.c_void_p,  # LPHWAVEIN / LPHWAVEOUT
                wintypes.UINT,  # uDeviceID
                ctypes.c_void_p,  # LPCWAVEFORMATEX
                ctypes.c_void_p,  # DWORD_PTR dwCallback
                ctypes.c_void_p,  # DWORD_PTR dwInstance
                wintypes.DWORD,  # fdwOpen
            ]
            fn.restype = wintypes.UINT
        for name in (
            "waveInPrepareHeader",
            "waveInUnprepareHeader",
            "waveInAddBuffer",
            "waveOutPrepareHeader",
            "waveOutUnprepareHeader",
            "waveOutWrite",
        ):
            fn = getattr(winmm, name)
            fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
            fn.restype = wintypes.UINT
        for name in ("waveInStart", "waveInStop", "waveInReset", "waveInClose"):
            fn = getattr(winmm, name)
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = wintypes.UINT
        for name in ("waveOutReset", "waveOutClose"):
            fn = getattr(winmm, name)
            fn.argtypes = [ctypes.c_void_p]
            fn.restype = wintypes.UINT
        self._configured = True

    def _device_caps(self, *, kind: str, index: int) -> str:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        class WAVEOUTCAPS(ctypes.Structure):
            _fields_ = [
                ("wMid", wintypes.WORD),
                ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32),
                ("dwFormats", wintypes.DWORD),
                ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD),
                ("dwSupport", wintypes.DWORD),
            ]

        class WAVEINCAPS(ctypes.Structure):
            _fields_ = [
                ("wMid", wintypes.WORD),
                ("wPid", wintypes.WORD),
                ("vDriverVersion", wintypes.UINT),
                ("szPname", wintypes.WCHAR * 32),
                ("dwFormats", wintypes.DWORD),
                ("wChannels", wintypes.WORD),
                ("wReserved1", wintypes.WORD),
            ]

        caps: Any = WAVEINCAPS() if kind == KIND_INPUT else WAVEOUTCAPS()
        getter = (
            self._winmm.waveInGetDevCapsW if kind == KIND_INPUT else self._winmm.waveOutGetDevCapsW
        )
        if getter(index, ctypes.byref(caps), ctypes.sizeof(caps)) == 0:
            return str(getattr(caps, "szPname", "") or "")
        return ""

    def available(self) -> tuple[bool, bool, str]:
        if self._winmm is None:  # not on Windows
            return (False, False, "the Windows audio API is only available on Windows")
        try:
            wave_in = self._winmm.waveInGetNumDevs()
            wave_out = self._winmm.waveOutGetNumDevs()
        except (AttributeError, OSError) as exc:  # pragma: no cover - Windows only
            return (False, False, f"the Windows audio API is unavailable: {exc}")
        if not wave_in and not wave_out:
            return (False, False, "no audio device is present on this machine")
        return (bool(wave_in), bool(wave_out), "")

    def list_devices(self) -> list[dict[str, str]]:  # pragma: no cover - Windows only
        devices: list[dict[str, str]] = []
        if self._winmm is None:
            return devices
        try:
            for index in range(self._winmm.waveInGetNumDevs()):
                name = self._device_caps(kind=KIND_INPUT, index=index) or f"Microphone {index}"
                devices.append({"id": f"in:{index}", "name": name, "kind": KIND_INPUT})
            for index in range(self._winmm.waveOutGetNumDevs()):
                name = self._device_caps(kind=KIND_OUTPUT, index=index) or f"Speaker {index}"
                devices.append({"id": f"out:{index}", "name": name, "kind": KIND_OUTPUT})
        except (AttributeError, OSError):
            logger.debug("could not enumerate Windows audio devices", exc_info=True)
        return devices

    # -- Capture ---------------------------------------------------------
    def _wave_format(self, *, sample_rate: int, channels: int) -> Any:  # pragma: no cover
        import ctypes
        from ctypes import wintypes

        class WAVEFORMATEX(ctypes.Structure):
            _fields_ = [
                ("wFormatTag", wintypes.WORD),
                ("nChannels", wintypes.WORD),
                ("nSamplesPerSec", wintypes.DWORD),
                ("nAvgBytesPerSec", wintypes.DWORD),
                ("nBlockAlign", wintypes.WORD),
                ("wBitsPerSample", wintypes.WORD),
                ("cbSize", wintypes.WORD),
            ]

        block_align = channels * 2  # 16-bit samples
        return WAVEFORMATEX(
            wFormatTag=WAVE_FORMAT_PCM,
            nChannels=channels,
            nSamplesPerSec=sample_rate,
            nAvgBytesPerSec=sample_rate * block_align,
            nBlockAlign=block_align,
            wBitsPerSample=16,
            cbSize=0,
        )

    def _wave_header(self, *, buffer: Any, length: int) -> Any:  # pragma: no cover
        import ctypes
        from ctypes import wintypes

        class WAVEHDR(ctypes.Structure):
            _fields_ = [
                ("lpData", ctypes.c_void_p),
                ("dwBufferLength", wintypes.DWORD),
                ("dwBytesRecorded", wintypes.DWORD),
                ("dwUser", ctypes.c_void_p),
                ("dwFlags", wintypes.DWORD),
                ("dwLoops", wintypes.DWORD),
                ("lpNext", ctypes.c_void_p),
                ("reserved", ctypes.c_void_p),
            ]

        header = WAVEHDR()
        header.lpData = ctypes.cast(buffer, ctypes.c_void_p)
        header.dwBufferLength = length
        return header

    def capture(
        self,
        *,
        device: str,
        seconds: float,
        sample_rate: int,
        channels: int,
        path: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:  # pragma: no cover - Windows only
        if self._winmm is None:
            raise ExternalServiceError("the Windows audio API is only available on Windows")
        self._configure()
        import ctypes

        winmm = self._winmm
        frames = max(1, int(seconds * sample_rate))
        length = frames * channels * 2  # 16-bit PCM
        wfx = self._wave_format(sample_rate=sample_rate, channels=channels)
        buffer = ctypes.create_string_buffer(length)
        header = self._wave_header(buffer=buffer, length=length)
        handle = ctypes.c_void_p()
        index = _device_index(device, default=WAVE_MAPPER)
        result = winmm.waveInOpen(
            ctypes.byref(handle),
            ctypes.c_uint(index),
            ctypes.byref(wfx),
            ctypes.c_void_p(0),
            ctypes.c_void_p(0),
            ctypes.c_uint(CALLBACK_NULL),
        )
        if result != 0:
            raise ExternalServiceError(
                "could not open the microphone",
                details={"device": device or "default", "waveInOpen": result},
            )
        try:
            if winmm.waveInPrepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header)) != 0:
                raise ExternalServiceError("could not prepare the capture buffer")
            if winmm.waveInAddBuffer(handle, ctypes.byref(header), ctypes.sizeof(header)) != 0:
                raise ExternalServiceError("could not queue the capture buffer")
            if winmm.waveInStart(handle) != 0:
                raise ExternalServiceError("could not start recording")
            deadline = time.monotonic() + seconds + _COMPLETION_GRACE
            cancelled = False
            while time.monotonic() < deadline:
                if header.dwFlags & WHDR_DONE:
                    break
                if should_cancel is not None and should_cancel():
                    cancelled = True
                    break
                time.sleep(0.02)
            winmm.waveInStop(handle)
            winmm.waveInReset(handle)
            recorded = int(header.dwBytesRecorded)
            if cancelled:
                # Keep whatever was captured before the stop, then report the
                # cancellation — a deliberate stop is not a failure.
                from ..cancel import CancelledError

                if recorded:
                    _write_wav(
                        path, buffer.raw[:recorded], sample_rate=sample_rate, channels=channels
                    )
                raise CancelledError("recording was stopped")
            if not header.dwFlags & WHDR_DONE:
                raise ExternalServiceError("the microphone produced no audio before the time limit")
            _write_wav(
                path,
                buffer.raw[: (recorded or length)],
                sample_rate=sample_rate,
                channels=channels,
            )
        finally:
            try:
                winmm.waveInUnprepareHeader(handle, ctypes.byref(header), ctypes.sizeof(header))
            except (AttributeError, OSError):
                logger.debug("could not unprepare the capture buffer", exc_info=True)
            winmm.waveInClose(handle)

    # -- Playback --------------------------------------------------------
    def playback(
        self,
        *,
        device: str,
        path: str,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:  # pragma: no cover - Windows only
        if self._winmm is None:
            raise ExternalServiceError("the Windows audio API is only available on Windows")
        self._configure()
        import ctypes
        import wave

        winmm = self._winmm
        try:
            with wave.open(path, "rb") as handle:
                channels = handle.getnchannels()
                sample_rate = handle.getframerate()
                width = handle.getsampwidth()
                frames = handle.readframes(handle.getnframes())
        except (OSError, wave.Error) as exc:
            raise ExternalServiceError(
                "could not read the audio file to play", details={"error": str(exc)}
            ) from exc
        if width != 2:
            # WinMM plays PCM we feed it; a non-16-bit clip is played through the
            # OS association instead of being silently mangled.
            self._play_via_shell(path)
            return
        wfx = self._wave_format(sample_rate=sample_rate, channels=channels)
        buffer = ctypes.create_string_buffer(frames, len(frames))
        header = self._wave_header(buffer=buffer, length=len(frames))
        handle_out = ctypes.c_void_p()
        index = _device_index(device, default=WAVE_MAPPER)
        result = winmm.waveOutOpen(
            ctypes.byref(handle_out),
            ctypes.c_uint(index),
            ctypes.byref(wfx),
            ctypes.c_void_p(0),
            ctypes.c_void_p(0),
            ctypes.c_uint(CALLBACK_NULL),
        )
        if result != 0:
            raise ExternalServiceError(
                "could not open the speaker",
                details={"device": device or "default", "waveOutOpen": result},
            )
        try:
            if (
                winmm.waveOutPrepareHeader(handle_out, ctypes.byref(header), ctypes.sizeof(header))
                != 0
            ):
                raise ExternalServiceError("could not prepare the playback buffer")
            if winmm.waveOutWrite(handle_out, ctypes.byref(header), ctypes.sizeof(header)) != 0:
                raise ExternalServiceError("could not start playback")
            duration = len(frames) / (sample_rate * channels * 2) if sample_rate else 0.0
            deadline = time.monotonic() + duration + _COMPLETION_GRACE
            while time.monotonic() < deadline:
                if header.dwFlags & WHDR_DONE:
                    break
                if should_cancel is not None and should_cancel():
                    # waveOutReset stops playback immediately; a stop is not a failure.
                    from ..cancel import CancelledError

                    winmm.waveOutReset(handle_out)
                    raise CancelledError("playback was stopped")
                time.sleep(0.02)
            winmm.waveOutReset(handle_out)
        finally:
            try:
                winmm.waveOutUnprepareHeader(
                    handle_out, ctypes.byref(header), ctypes.sizeof(header)
                )
            except (AttributeError, OSError):
                logger.debug("could not unprepare the playback buffer", exc_info=True)
            winmm.waveOutClose(handle_out)

    def _play_via_shell(self, path: str) -> None:  # pragma: no cover - Windows only
        import winsound

        play = getattr(winsound, "PlaySound", None)
        flag = getattr(winsound, "SND_FILENAME", 0)
        if play is None:
            raise ExternalServiceError("Windows playback is unavailable")
        try:
            play(path, flag)
        except RuntimeError as exc:
            raise ExternalServiceError(
                "Windows playback failed", details={"error": str(exc)}
            ) from exc


def _write_wav(path: str, frames: bytes, *, sample_rate: int, channels: int) -> None:
    """Write 16-bit PCM ``frames`` as a standard WAV file."""
    import wave

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(frames)


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
                    is_default=not raw.get("id"),
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
        should_cancel: Callable[[], bool] | None = None,
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
            should_cancel=should_cancel,
        )
        if not _file_exists(output_path):
            raise ExternalServiceError("the recorder produced no file")
        return _clip_from_wav(
            output_path, engine=self.name, sample_rate=sample_rate_hz, channels=channels
        )

    async def play(
        self,
        *,
        clip_path: str,
        device_id: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.require_available()
        if not self._can_play:
            raise ExternalServiceError(
                "playback is not available on this machine",
                details={"reason": "no render device"},
            )
        if not _file_exists(clip_path):
            raise NotFoundError("the audio file to play does not exist")
        await asyncio.to_thread(
            self._adapter.playback,
            device=device_id,
            path=clip_path,
            should_cancel=should_cancel,
        )


def _file_exists(path: str) -> bool:
    """Existence check in a sync helper.

    Kept out of the async bodies so the event loop is not asked to perform a
    blocking filesystem call; the check itself is a single ``stat``.
    """
    return os.path.exists(path)


__all__ = ["CtypesWinAudioAdapter", "WinAudioAdapter", "WindowsAudioBackend"]
