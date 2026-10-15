"""Local (offline) speech engines.

The offline half of the speech capability. Both engines are optional extras: when
the package is absent the engine reports itself unavailable with the install hint
rather than failing, so a build without them degrades honestly (the capability
report says transcription is unavailable) instead of pretending.

* :class:`LocalWhisperTranscriber` — speech-to-text with ``faster-whisper``
  (CTranslate2). The model is loaded lazily and cached per instance; loading it
  is a real, observable cost, so it happens on first use, not at construction.
* :class:`PiperSynthesizer` — text-to-speech with the ``piper`` neural TTS. It
  prefers an installed ``piper`` binary and falls back to the ``piper`` Python
  package. The voice model is a local file; nothing leaves the machine.

Both engines run entirely on the user's machine, which is what makes the offline
voice path real rather than aspirational.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

from ..base import (
    FORMAT_WAV,
    AudioClip,
    SpeechEngineError,
    SynthesisEngine,
    SynthesisResult,
    TranscriptionEngine,
    TranscriptionResult,
    TranscriptionSegment,
)

logger = logging.getLogger(__name__)


class LocalWhisperTranscriber(TranscriptionEngine):
    """Offline speech-to-text via ``faster-whisper``."""

    name = "faster-whisper"
    is_cloud = False
    languages = ("ar", "en")

    def __init__(self, *, model_size: str = "small", device: str = "cpu") -> None:
        self._model_size = model_size
        self._device = device
        self._model: object | None = None
        try:
            import faster_whisper  # noqa: F401

            self.available = True
        except ImportError:
            self.available = False
            self.unavailable_reason = (
                "the faster-whisper package is not installed (pip install faster-whisper)"
            )

    def _load(self) -> object:
        if self._model is None:
            from faster_whisper import WhisperModel

            self._model = WhisperModel(self._model_size, device=self._device, compute_type="int8")
        return self._model

    def _transcribe_sync(self, audio_path: str, language: str, prompt: str) -> TranscriptionResult:
        model = self._load()
        try:
            segments, info = model.transcribe(  # type: ignore[attr-defined]
                audio_path,
                language=language or None,
                initial_prompt=prompt or None,
            )
        except Exception as exc:
            raise SpeechEngineError(
                "local transcription failed", details={"engine": self.name, "error": str(exc)}
            ) from exc
        collected: list[TranscriptionSegment] = []
        texts: list[str] = []
        for segment in segments:
            collected.append(
                TranscriptionSegment(
                    start_seconds=float(getattr(segment, "start", 0.0)),
                    end_seconds=float(getattr(segment, "end", 0.0)),
                    text=str(getattr(segment, "text", "")).strip(),
                )
            )
            texts.append(str(getattr(segment, "text", "")).strip())
        return TranscriptionResult(
            text=" ".join(t for t in texts if t).strip(),
            language=getattr(info, "language", language) or language,
            segments=tuple(collected),
            engine=self.name,
            duration_seconds=float(getattr(info, "duration", 0.0) or 0.0),
            confidence=None,
        )

    async def transcribe(
        self, *, audio_path: str, language: str = "ar", prompt: str = ""
    ) -> TranscriptionResult:
        self.require_available()
        if not _file_exists(audio_path):
            raise SpeechEngineError(
                "the audio file to transcribe does not exist", details={"path": audio_path}
            )
        return await asyncio.to_thread(self._transcribe_sync, audio_path, language, prompt)


class PiperSynthesizer(SynthesisEngine):
    """Offline text-to-speech via the ``piper`` neural TTS.

    Prefers a ``piper`` binary on ``PATH`` (the packaged form) and falls back to
    the ``piper`` Python package. Either way a voice model file is required; when
    none is configured the engine reports unavailable rather than producing
    silence.
    """

    name = "piper"
    is_cloud = False
    languages = ("ar", "en")

    def __init__(self, *, voice_model: str = "", sample_rate_hz: int = 22050) -> None:
        self._voice_model = voice_model
        self._sample_rate_hz = sample_rate_hz
        self._binary = shutil.which("piper")
        self._module = _importable("piper")
        if not self._binary and not self._module:
            self.available = False
            self.unavailable_reason = (
                "the piper TTS engine is not installed (pip install piper-tts, "
                "or put the piper binary on PATH)"
            )
        elif not voice_model:
            self.available = False
            self.unavailable_reason = "no piper voice model is configured"
        else:
            self.available = True
        if voice_model:
            self.voices = (Path(voice_model).stem,)

    def _synthesize_sync(self, text: str, output_path: str) -> None:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if self._binary:
            self._synthesize_binary(text, output_path)
        else:
            self._synthesize_module(text, output_path)

    def _synthesize_binary(self, text: str, output_path: str) -> None:
        import subprocess

        argv = [self._binary or "piper", "--model", self._voice_model, "--output_file", output_path]
        try:
            # No shell; the text is fed on stdin, never as an argument, so it can
            # never be interpreted as a flag or a command.
            result = subprocess.run(  # noqa: S603
                argv, input=text.encode("utf-8"), capture_output=True, timeout=120, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SpeechEngineError(
                "local synthesis failed", details={"engine": self.name, "error": str(exc)}
            ) from exc
        if result.returncode != 0:
            raise SpeechEngineError(
                "local synthesis failed",
                details={
                    "engine": self.name,
                    "returncode": result.returncode,
                    "stderr": result.stderr.decode("utf-8", errors="ignore")[:500],
                },
            )

    def _synthesize_module(self, text: str, output_path: str) -> None:
        try:
            from piper import PiperVoice
        except ImportError as exc:  # pragma: no cover - guarded by availability
            raise SpeechEngineError(
                "the piper Python package is not usable", details={"error": str(exc)}
            ) from exc
        try:
            import wave

            voice = PiperVoice.load(self._voice_model)
            with wave.open(output_path, "wb") as handle:
                voice.synthesize(text, handle)
        except Exception as exc:
            raise SpeechEngineError(
                "local synthesis failed", details={"engine": self.name, "error": str(exc)}
            ) from exc

    async def synthesize(
        self, *, text: str, output_path: str, voice: str = "", language: str = "ar"
    ) -> SynthesisResult:
        self.require_available()
        await asyncio.to_thread(self._synthesize_sync, text, output_path)
        clip = _wav_clip(output_path, engine=self.name, sample_rate=self._sample_rate_hz)
        return SynthesisResult(
            clip=clip,
            voice=voice or Path(self._voice_model).stem,
            engine=self.name,
            language=language,
        )


def _file_exists(path: str) -> bool:
    """Existence check in a sync helper.

    Kept out of the async bodies so the event loop is not asked to perform a
    blocking filesystem call; the check itself is a single ``stat``.
    """
    return os.path.exists(path)


def _wav_clip(path: str, *, engine: str, sample_rate: int) -> AudioClip:
    import wave

    from ...core.ids import PREFIX_AUDIO, new_id

    duration = 0.0
    size = 0
    try:
        size = Path(path).stat().st_size
        with wave.open(path, "rb") as handle:
            rate = handle.getframerate() or sample_rate
            frames = handle.getnframes()
            sample_rate = rate
            duration = frames / rate if rate else 0.0
    except (OSError, wave.Error):
        logger.debug("could not read synthesized WAV header for %s", path, exc_info=True)
    return AudioClip(
        id=new_id(PREFIX_AUDIO),
        path=path,
        format=FORMAT_WAV,
        sample_rate_hz=sample_rate,
        duration_seconds=duration,
        size_bytes=size,
        engine=engine,
    )


def _importable(name: str) -> bool:
    import importlib.util

    return importlib.util.find_spec(name) is not None


__all__ = ["LocalWhisperTranscriber", "PiperSynthesizer"]
