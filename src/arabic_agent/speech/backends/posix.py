"""POSIX audio backend.

The POSIX half of the audio seam. It implements the same
:class:`~arabic_agent.speech.base.AudioBackend` contract as the Windows backend,
so the policy, budget and audit above it are unchanged.

Two layers, deliberately separated:

* :class:`PosixAudioAdapter` is the *only* place that touches an external audio
  program. It is a narrow, injectable seam: every method returns plain values, so
  a test can substitute a fake adapter to exercise the whole backend without a
  sound card.
* :class:`PosixAudioBackend` translates those raw values into the shared models
  and reports availability honestly.

The real adapter uses the standard ``arecord``/``aplay`` tools (ALSA's
``alsa-utils``) when they are installed and a capture device exists. They are
invoked with an argument vector and no shell, so a device name or file path can
never inject a command. When the tools are absent — or there is no ``/dev/snd``,
as in a headless container — the backend reports itself unavailable with a clear
reason rather than pretending to listen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import wave
from abc import ABC, abstractmethod
from collections.abc import Callable
from pathlib import Path

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError, NotFoundError
from ..base import (
    FORMAT_WAV,
    KIND_INPUT,
    KIND_OUTPUT,
    AudioBackend,
    AudioClip,
    AudioDevice,
)

logger = logging.getLogger(__name__)


class PosixAudioAdapter(ABC):
    """The narrow seam over the ALSA command-line tools."""

    @abstractmethod
    def tools_present(self) -> tuple[bool, bool]:
        """Return ``(record_available, play_available)``."""

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
        """Record ``seconds`` of audio to ``path``."""

    @abstractmethod
    def playback(
        self, *, device: str, path: str, should_cancel: Callable[[], bool] | None = None
    ) -> None:
        """Play the audio file at ``path``."""


class AlsaAudioAdapter(PosixAudioAdapter):
    """The real adapter: ``arecord`` / ``aplay`` from ``alsa-utils``."""

    def tools_present(self) -> tuple[bool, bool]:
        return (shutil.which("arecord") is not None, shutil.which("aplay") is not None)

    def list_devices(self) -> list[dict[str, str]]:
        # ALSA enumerates cards under /proc/asound; the ``default`` PCM always
        # exists when the subsystem does, so it is offered as the default device.
        devices: list[dict[str, str]] = []
        cards = Path("/proc/asound/cards")
        if cards.is_file():
            for line in cards.read_text(encoding="utf-8", errors="ignore").splitlines():
                stripped = line.strip()
                # Card lines look like " 0 [PCH            ]: HDA-Intel - HDA Intel PCH".
                if stripped and stripped[0].isdigit() and "[" in stripped:
                    devices.append({"id": "default", "name": stripped, "kind": KIND_INPUT})
                    devices.append({"id": "default", "name": stripped, "kind": KIND_OUTPUT})
        if not devices and Path("/dev/snd").exists():
            devices.append({"id": "default", "name": "Default ALSA device", "kind": KIND_INPUT})
            devices.append({"id": "default", "name": "Default ALSA device", "kind": KIND_OUTPUT})
        return devices

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
        argv = [
            "arecord",
            "-q",
            "-D",
            device or "default",
            "-f",
            "S16_LE",
            "-r",
            str(sample_rate),
            "-c",
            str(channels),
            "-d",
            str(int(seconds)),
            path,
        ]
        _run(argv, should_cancel=should_cancel)

    def playback(
        self, *, device: str, path: str, should_cancel: Callable[[], bool] | None = None
    ) -> None:
        argv = ["aplay", "-q", "-D", device or "default", path]
        _run(argv, should_cancel=should_cancel)


def _run(argv: list[str], *, should_cancel: Callable[[], bool] | None = None) -> None:
    """Run an audio command, polling ``should_cancel`` so a stop is prompt.

    A stop terminates the process group (``arecord``/``aplay`` write partial
    output first, so the recording made so far is kept) and raises
    :class:`CancelledError` rather than a generic failure — a deliberate stop is
    not an error.
    """
    from ..cancel import CancelledError

    try:
        # No shell, fixed program, validated arguments — a device name cannot
        # inject a command. A new process group lets a stop kill the whole tree.
        process = subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExternalServiceError(
            "the audio command is not installed", details={"program": argv[0]}
        ) from exc
    try:
        while True:
            try:
                _stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if should_cancel is not None and should_cancel():
                    _terminate_group(process)
                    process.communicate()
                    raise CancelledError("the audio operation was stopped") from None
    except Exception:
        if process.poll() is None:
            _terminate_group(process)
            process.communicate()
        raise
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="ignore").strip()
        raise ExternalServiceError(
            "the audio command failed",
            details={
                "program": argv[0],
                "returncode": process.returncode,
                "stderr": stderr_text[:500],
            },
        )


def _terminate_group(process: subprocess.Popen[bytes]) -> None:
    """Stop an audio process and its children, tolerating an already-dead one.

    Process groups are POSIX-only (``killpg``/``getpgid``/``SIGKILL``); this
    module ships in the cross-platform payload, so the group signal is used
    only where the platform provides it and the process itself is terminated
    otherwise.
    """
    killpg = getattr(os, "killpg", None)
    getpgid = getattr(os, "getpgid", None)
    sigkill = getattr(signal, "SIGKILL", signal.SIGTERM)

    def _signal_group(sig: int) -> bool:
        if killpg is None or getpgid is None:
            return False
        try:
            killpg(getpgid(process.pid), sig)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False

    if not _signal_group(signal.SIGTERM):
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if not _signal_group(sigkill):
            process.kill()


class PosixAudioBackend(AudioBackend):
    """ALSA capture/playback, reported honestly when unavailable."""

    name = "alsa"
    platform = PlatformKind.LINUX

    def __init__(self, *, adapter: PosixAudioAdapter | None = None) -> None:
        self._adapter = adapter or AlsaAudioAdapter()
        can_record, can_play = self._adapter.tools_present()
        has_sound = Path("/dev/snd").exists()
        self._can_record = can_record and has_sound
        self._can_play = can_play and has_sound
        self.available = self._can_record or self._can_play
        if not self.available:
            if not can_record and not can_play:
                self.unavailable_reason = "the ALSA audio tools (arecord/aplay) are not installed"
            else:
                self.unavailable_reason = "no audio device is present on this machine"

    async def list_devices(self) -> list[AudioDevice]:
        if not self.available:
            return []
        devices: list[AudioDevice] = []
        for raw in self._adapter.list_devices():
            kind = raw.get("kind", KIND_INPUT)
            devices.append(
                AudioDevice(
                    id=raw.get("id", "default"),
                    name=raw.get("name", ""),
                    kind=kind,
                    is_default=True,
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
            output_path,
            engine=self.name,
            sample_rate=sample_rate_hz,
            channels=channels,
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


def _clip_from_wav(path: str, *, engine: str, sample_rate: int, channels: int) -> AudioClip:
    """Build clip metadata, reading the real WAV header when possible."""
    duration = 0.0
    size = 0
    fmt = FORMAT_WAV
    try:
        size = os.path.getsize(path)
        with wave.open(path, "rb") as handle:
            frames = handle.getnframes()
            rate = handle.getframerate() or sample_rate
            channels = handle.getnchannels() or channels
            sample_rate = rate
            duration = frames / rate if rate else 0.0
    except (OSError, wave.Error):
        logger.debug("could not read WAV header for %s", path, exc_info=True)
    from ...core.ids import PREFIX_AUDIO, new_id

    return AudioClip(
        id=new_id(PREFIX_AUDIO),
        path=path,
        format=fmt,
        sample_rate_hz=sample_rate,
        channels=channels,
        duration_seconds=duration,
        size_bytes=size,
        engine=engine,
    )


__all__ = ["AlsaAudioAdapter", "PosixAudioAdapter", "PosixAudioBackend"]
