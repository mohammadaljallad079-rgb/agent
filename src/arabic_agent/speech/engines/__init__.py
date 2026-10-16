"""Speech engine selection.

One place chooses the speech-to-text and text-to-speech engines for the current
configuration, mirroring how ``printing/backends`` chooses a print backend. The
choice is *explicit and honest*:

* a local engine is preferred whenever it is installed, so the default voice path
  never sends audio off the machine;
* a cloud engine is built only when the operator enabled cloud speech **and**
  supplied an endpoint — a local-only or offline runtime can never accidentally
  construct one;
* when nothing is installed the null engine is returned carrying the reason, so
  the capability report says "transcription unavailable" instead of the run
  failing halfway.

The local engines are optional extras; their absence is not an error, it is a
degraded capability that the UI and the API both surface.
"""

from __future__ import annotations

from ..base import SynthesisEngine, TranscriptionEngine
from .cloud import OpenAICompatibleSynthesizer, OpenAICompatibleTranscriber
from .local import LocalWhisperTranscriber, PiperSynthesizer
from .null import NullSynthesisEngine, NullTranscriptionEngine
from .windows import WindowsSapiSynthesizer


def build_transcription_engine(
    *,
    engine: str = "auto",
    allow_cloud: bool = False,
    cloud_base_url: str = "",
    cloud_api_key: str = "",
    cloud_model: str = "whisper-1",
    whisper_model_size: str = "small",
    whisper_device: str = "cpu",
    whisper_model_path: str = "",
) -> TranscriptionEngine:
    """Return the best available speech-to-text engine.

    ``whisper_model_path`` is an installed CTranslate2 model directory; when set
    it wins over ``whisper_model_size`` so the product loads the artifact it
    installed rather than whatever ``faster-whisper`` would otherwise fetch.
    """
    if engine in {"auto", "local", "faster-whisper"}:
        local = LocalWhisperTranscriber(
            model_size=whisper_model_size,
            device=whisper_device,
            model_path=whisper_model_path,
        )
        if local.available:
            return local
        if engine in {"local", "faster-whisper"}:
            return NullTranscriptionEngine(local.unavailable_reason)
    if engine in {"auto", "cloud"} and allow_cloud and cloud_base_url:
        cloud = OpenAICompatibleTranscriber(
            base_url=cloud_base_url, api_key=cloud_api_key, model=cloud_model
        )
        if cloud.available:
            return cloud
    if engine == "cloud":
        return NullTranscriptionEngine("cloud speech is disabled or no endpoint is configured")
    return NullTranscriptionEngine("no speech-to-text engine is installed")


def build_synthesis_engine(
    *,
    engine: str = "auto",
    allow_cloud: bool = False,
    cloud_base_url: str = "",
    cloud_api_key: str = "",
    cloud_model: str = "tts-1",
    cloud_voice: str = "alloy",
    piper_voice_model: str = "",
) -> SynthesisEngine:
    """Return the best available text-to-speech engine.

    The preference order for ``auto`` is deliberate: a configured Piper voice
    first (a small, offline neural voice), then the voices already installed in
    Windows (SAPI5, no download), then a cloud engine when explicitly allowed.
    """
    if engine in {"auto", "local", "piper"}:
        if piper_voice_model:
            local = PiperSynthesizer(voice_model=piper_voice_model)
            if local.available:
                return local
            if engine in {"local", "piper"}:
                return NullSynthesisEngine(local.unavailable_reason)
        elif engine == "piper":
            return NullSynthesisEngine("no piper voice model is configured")
    if engine in {"auto", "sapi", "windows"}:
        sapi = WindowsSapiSynthesizer()
        if sapi.available:
            return sapi
        if engine in {"sapi", "windows"}:
            return NullSynthesisEngine(sapi.unavailable_reason)
    if engine in {"auto", "cloud"} and allow_cloud and cloud_base_url:
        cloud = OpenAICompatibleSynthesizer(
            base_url=cloud_base_url,
            api_key=cloud_api_key,
            model=cloud_model,
            default_voice=cloud_voice,
        )
        if cloud.available:
            return cloud
    if engine == "cloud":
        return NullSynthesisEngine("cloud speech is disabled or no endpoint is configured")
    return NullSynthesisEngine("no text-to-speech engine is installed")


__all__ = ["build_synthesis_engine", "build_transcription_engine"]
