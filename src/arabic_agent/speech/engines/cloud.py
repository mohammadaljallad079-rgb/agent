"""Cloud speech engines.

The online half of the speech capability, reached only through an
OpenAI-compatible audio API. These engines are *never* constructed unless the
operator has explicitly enabled cloud speech and the caller consented for the
specific request — the container refuses to build them otherwise, so a local-only
or offline runtime cannot accidentally send a recording off the machine.

* :class:`OpenAICompatibleTranscriber` — speech-to-text via
  ``POST {base_url}/audio/transcriptions``.
* :class:`OpenAICompatibleSynthesizer` — text-to-speech via
  ``POST {base_url}/audio/speech``.

Both use ``httpx`` directly (no vendor SDK), so the endpoint is a base URL and the
same code works against OpenAI, Azure OpenAI, a local OpenAI-compatible server,
or a self-hosted gateway. Failures are translated into a classified
:class:`SpeechEngineError` so the agent loop can recover (retry, fall back to a
local engine, or ask the user).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import httpx

from ..base import (
    FORMAT_MP3,
    SpeechEngineError,
    SynthesisEngine,
    SynthesisResult,
    TranscriptionEngine,
    TranscriptionResult,
)

logger = logging.getLogger(__name__)


class OpenAICompatibleTranscriber(TranscriptionEngine):
    """Cloud speech-to-text against an OpenAI-compatible endpoint."""

    name = "openai-compatible-stt"
    is_cloud = True
    languages = ("ar", "en")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "whisper-1",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._client = client
        self.available = bool(self._base_url)
        if not self.available:
            self.unavailable_reason = "no cloud speech endpoint is configured"

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def transcribe(
        self, *, audio_path: str, language: str = "ar", prompt: str = ""
    ) -> TranscriptionResult:
        self.require_available()
        path = Path(audio_path)
        if not _file_exists(audio_path):
            raise SpeechEngineError(
                "the audio file to transcribe does not exist", details={"path": audio_path}
            )
        data = {"model": self._model}
        if language:
            data["language"] = language
        if prompt:
            data["prompt"] = prompt
        # Read the file off the event loop; the runtime already bounded its size.
        audio_bytes = await asyncio.to_thread(path.read_bytes)
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns = self._client is None
        try:
            response = await client.post(
                f"{self._base_url}/audio/transcriptions",
                headers=self._headers(),
                data=data,
                files={"file": (path.name, audio_bytes, "application/octet-stream")},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise SpeechEngineError(
                "cloud transcription was rejected",
                details={"engine": self.name, "status": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SpeechEngineError(
                "cloud transcription failed", details={"engine": self.name, "error": str(exc)}
            ) from exc
        finally:
            if owns:
                await client.aclose()
        text = str(payload.get("text", "")).strip() if isinstance(payload, dict) else ""
        return TranscriptionResult(
            text=text,
            language=language,
            engine=self.name,
            confidence=None,
        )


class OpenAICompatibleSynthesizer(SynthesisEngine):
    """Cloud text-to-speech against an OpenAI-compatible endpoint."""

    name = "openai-compatible-tts"
    is_cloud = True
    languages = ("ar", "en")

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        model: str = "tts-1",
        default_voice: str = "alloy",
        timeout: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._default_voice = default_voice
        self._timeout = timeout
        self._client = client
        self.voices = (default_voice,)
        self.available = bool(self._base_url)
        if not self.available:
            self.unavailable_reason = "no cloud speech endpoint is configured"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "audio/mpeg"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def synthesize(
        self, *, text: str, output_path: str, voice: str = "", language: str = "ar"
    ) -> SynthesisResult:
        self.require_available()
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        owns = self._client is None
        try:
            response = await client.post(
                f"{self._base_url}/audio/speech",
                headers=self._headers(),
                json={
                    "model": self._model,
                    "input": text,
                    "voice": voice or self._default_voice,
                    "response_format": "mp3",
                },
            )
            response.raise_for_status()
            await asyncio.to_thread(Path(output_path).write_bytes, response.content)
        except httpx.HTTPStatusError as exc:
            raise SpeechEngineError(
                "cloud synthesis was rejected",
                details={"engine": self.name, "status": exc.response.status_code},
            ) from exc
        except (httpx.HTTPError, OSError) as exc:
            raise SpeechEngineError(
                "cloud synthesis failed", details={"engine": self.name, "error": str(exc)}
            ) from exc
        finally:
            if owns:
                await client.aclose()
        from ...core.ids import PREFIX_AUDIO, new_id
        from ..base import AudioClip

        size_bytes = await asyncio.to_thread(lambda: Path(output_path).stat().st_size)
        clip = AudioClip(
            id=new_id(PREFIX_AUDIO),
            path=output_path,
            format=FORMAT_MP3,
            duration_seconds=0.0,
            size_bytes=size_bytes,
            engine=self.name,
            language=language,
        )
        return SynthesisResult(
            clip=clip,
            voice=voice or self._default_voice,
            engine=self.name,
            language=language,
        )


def _file_exists(path: str) -> bool:
    """Existence check in a sync helper.

    Kept out of the async bodies so the event loop is not asked to perform a
    blocking filesystem call; the check itself is a single ``stat``.
    """
    return os.path.exists(path)


__all__ = ["OpenAICompatibleSynthesizer", "OpenAICompatibleTranscriber"]
