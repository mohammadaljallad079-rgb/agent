"""Scripted speech doubles for tests.

These perform no real device or model I/O, but they do write *real* audio files
to disk so the runtime's size checks, the sandbox path handling and the durable
store are exercised against genuine files rather than mocked return values. They
are only ever injected explicitly, never selected by the factories in
``arabic_agent.speech.backends`` / ``engines`` — so a test can never mistake a
double for a working microphone.
"""

from __future__ import annotations

import os
import wave
from collections.abc import Callable
from pathlib import Path

from arabic_agent.core.enums import PlatformKind
from arabic_agent.core.errors import ExternalServiceError
from arabic_agent.speech.base import (
    FORMAT_WAV,
    AudioBackend,
    AudioClip,
    AudioDevice,
    SpeechEngineError,
    SynthesisEngine,
    SynthesisResult,
    TranscriptionEngine,
    TranscriptionResult,
    TranscriptionSegment,
)


def _file_exists(path: str) -> bool:
    return os.path.exists(path)


def _write_wav(path: str, *, seconds: float, sample_rate: int, channels: int) -> int:
    """Write a real (silent) PCM WAV file and return its byte size."""
    frames = max(1, int(seconds * sample_rate))
    with wave.open(path, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * frames * channels)
    return Path(path).stat().st_size


class FakeAudioBackend(AudioBackend):
    name = "fake"
    platform = PlatformKind.LINUX
    available = True

    def __init__(
        self,
        *,
        devices: list[AudioDevice] | None = None,
        fail_record: bool = False,
        fail_play: bool = False,
        block_until_cancelled: bool = False,
    ) -> None:
        self._devices = devices or [
            AudioDevice(id="default", name="Default Mic", kind="input", is_default=True),
            AudioDevice(id="default", name="Default Speaker", kind="output", is_default=True),
        ]
        self._fail_record = fail_record
        self._fail_play = fail_play
        #: Model a real blocking device loop: ``record``/``play`` hold until the
        #: cancellation callable returns True, then raise ``CancelledError``. Runs
        #: in a worker thread (like the POSIX/Windows adapters) so the event loop
        #: stays free for the stop request to arrive.
        self._block_until_cancelled = block_until_cancelled
        self.recorded: list[tuple[str, float]] = []
        self.played: list[str] = []

    def _poll_cancel(self, should_cancel: Callable[[], bool] | None) -> None:
        # Mirrors the real backend loops: consult the cancellation callable and
        # raise the same ``CancelledError`` the runtime translates into a
        # ``cancelled`` speech action.
        if should_cancel is not None and should_cancel():
            from arabic_agent.speech.cancel import CancelledError

            raise CancelledError("stopped by the test")

    @staticmethod
    def _block(should_cancel: Callable[[], bool] | None) -> None:
        import time

        from arabic_agent.speech.cancel import CancelledError

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if should_cancel is not None and should_cancel():
                raise CancelledError("stopped by the test")
            time.sleep(0.005)
        raise AssertionError("the test never cancelled the blocked operation")

    async def _maybe_block(self, should_cancel: Callable[[], bool] | None) -> None:
        if self._block_until_cancelled:
            import asyncio

            await asyncio.to_thread(self._block, should_cancel)

    async def list_devices(self) -> list[AudioDevice]:
        return list(self._devices)

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
        if self._fail_record:
            raise ExternalServiceError("the microphone could not be opened")
        self._poll_cancel(should_cancel)
        await self._maybe_block(should_cancel)
        size = _write_wav(
            output_path, seconds=seconds, sample_rate=sample_rate_hz, channels=channels
        )
        self.recorded.append((output_path, seconds))
        return AudioClip(
            id="aud_test",
            path=output_path,
            format=FORMAT_WAV,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
            duration_seconds=seconds,
            size_bytes=size,
            engine=self.name,
        )

    async def play(
        self,
        *,
        clip_path: str,
        device_id: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.require_available()
        if self._fail_play:
            raise ExternalServiceError("the speaker could not be opened")
        self._poll_cancel(should_cancel)
        await self._maybe_block(should_cancel)
        if not _file_exists(clip_path):
            from arabic_agent.core.errors import NotFoundError

            raise NotFoundError("the audio file to play does not exist")
        self.played.append(clip_path)


class FakeUnavailableAudioBackend(AudioBackend):
    name = "none"
    platform = PlatformKind.UNKNOWN
    available = False
    unavailable_reason = "no audio backend is implemented for this platform"

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


class FakeTranscriber(TranscriptionEngine):
    name = "fake-stt"
    available = True
    languages = ("ar", "en")

    def __init__(self, *, text: str = "مرحبا", is_cloud: bool = False, fail: bool = False) -> None:
        self._text = text
        self.is_cloud = is_cloud
        self._fail = fail
        self.calls: list[tuple[str, str]] = []

    async def transcribe(
        self, *, audio_path: str, language: str = "ar", prompt: str = ""
    ) -> TranscriptionResult:
        self.require_available()
        if self._fail:
            raise SpeechEngineError("the speech-to-text engine failed")
        self.calls.append((audio_path, language))
        return TranscriptionResult(
            text=self._text,
            language=language,
            engine=self.name,
            duration_seconds=1.0,
            segments=(TranscriptionSegment(start_seconds=0.0, end_seconds=1.0, text=self._text),),
        )


class FakeSynthesizer(SynthesisEngine):
    name = "fake-tts"
    available = True
    languages = ("ar", "en")
    voices = ("default",)

    def __init__(self, *, is_cloud: bool = False, fail: bool = False) -> None:
        self.is_cloud = is_cloud
        self._fail = fail
        self.calls: list[str] = []

    async def synthesize(
        self,
        *,
        text: str,
        output_path: str,
        voice: str = "",
        language: str = "ar",
    ) -> SynthesisResult:
        self.require_available()
        if self._fail:
            raise SpeechEngineError("the text-to-speech engine failed")
        self.calls.append(text)
        size = _write_wav(output_path, seconds=1.0, sample_rate=16000, channels=1)
        return SynthesisResult(
            clip=AudioClip(
                id="aud_test",
                path=output_path,
                format=FORMAT_WAV,
                duration_seconds=1.0,
                size_bytes=size,
                engine=self.name,
                language=language,
            ),
            voice=voice or "default",
            engine=self.name,
            language=language,
        )


class NullTranscriber(TranscriptionEngine):
    name = "none"
    available = False
    unavailable_reason = "no speech-to-text engine is installed"

    async def transcribe(
        self, *, audio_path: str, language: str = "ar", prompt: str = ""
    ) -> TranscriptionResult:
        self.require_available()
        raise AssertionError("unreachable")


class NullSynthesizer(SynthesisEngine):
    name = "none"
    available = False
    unavailable_reason = "no text-to-speech engine is installed"

    async def synthesize(
        self, *, text: str, output_path: str, voice: str = "", language: str = "ar"
    ) -> SynthesisResult:
        self.require_available()
        raise AssertionError("unreachable")


__all__ = [
    "FakeAudioBackend",
    "FakeSynthesizer",
    "FakeTranscriber",
    "FakeUnavailableAudioBackend",
    "NullSynthesizer",
    "NullTranscriber",
]
