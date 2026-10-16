"""Speech-layer data contracts and the three seams voice is built on.

The agent must never touch a raw audio API. Three small interfaces isolate every
piece of machine- or vendor-specific behaviour, so the runtime, the tools and the
agent core stay identical whether the microphone is a real ALSA device, the
speech-to-text model is local or in the cloud, or there is no audio at all:

    AudioBackend        record(...)  play(...)  list_devices()
    TranscriptionEngine transcribe(audio) -> text
    SynthesisEngine     synthesize(text)  -> audio

A backend or engine declares whether it is *available* on the current machine
and, if not, *why* — an unavailable one refuses with a clear reason instead of
pretending to listen or speak. Everything crossing these seams is a plain,
serialisable model, so it can be redacted and audited: audio is passed as a file
that already lives inside the sandbox, never as raw bytes from the model.

The cloud engines are deliberately *not* raw vendor SDKs. They are thin adapters
that route through the platform's :class:`~arabic_agent.models.gateway.ModelGateway`
with an :class:`~arabic_agent.core.enums.ModelCapability.AUDIO` model, so the
model gateway remains the single place a model is chosen, budgeted and audited.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from ..core.enums import PlatformKind
from ..core.errors import ExternalServiceError


class SpeechEngineError(ExternalServiceError):
    """A speech engine failed in a way the agent loop can classify and recover.

    Kept distinct from a generic external error so recovery can treat a failed
    transcription or synthesis as retryable-with-alternative-engine rather than
    as a hard failure of the run.
    """


#: Container formats this layer can name. WAV is the lingua franca every engine
#: and OS accepts losslessly, so it is the default the local path produces.
FORMAT_WAV = "wav"
FORMAT_FLAC = "flac"
FORMAT_OGG = "ogg"
FORMAT_MP3 = "mp3"
FORMAT_PCM = "pcm"

#: Audio-device direction. A capture endpoint is an input, a render endpoint an
#: output; the two are separately grantable permissions.
KIND_INPUT = "input"
KIND_OUTPUT = "output"


class AudioDevice(BaseModel):
    """One audio endpoint the machine exposes.

    ``available`` is reported, not assumed: a device that is enumerated but
    cannot be opened (busy, unplugged, permission denied) carries a reason
    rather than being hidden or claimed usable.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    name: str = ""
    kind: str = KIND_INPUT
    is_default: bool = False
    channels: int = 0
    sample_rate_hz: int = 0
    available: bool = True
    unavailable_reason: str = ""


class AudioClip(BaseModel):
    """A recorded or synthesized audio file that lives inside the sandbox.

    Only safe metadata crosses the boundary; the bytes stay on disk. ``path`` is
    a sandbox-relative name the runtime resolved — a clip can never point at an
    arbitrary host file.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    path: str
    format: str = FORMAT_WAV
    sample_rate_hz: int = 16000
    channels: int = 1
    duration_seconds: float = 0.0
    size_bytes: int = 0
    engine: str = ""
    language: str = ""


class TranscriptionSegment(BaseModel):
    """A time-stamped span of recognised speech, for provenance and subtitles."""

    model_config = ConfigDict(frozen=True)

    start_seconds: float = 0.0
    end_seconds: float = 0.0
    text: str = ""


class TranscriptionResult(BaseModel):
    """The outcome of transcribing one clip."""

    model_config = ConfigDict(frozen=True)

    text: str
    language: str = ""
    segments: tuple[TranscriptionSegment, ...] = ()
    engine: str = ""
    duration_seconds: float = 0.0
    #: ``None`` when the engine does not report confidence — never a guessed 1.0.
    confidence: float | None = None


class SynthesisResult(BaseModel):
    """The outcome of synthesizing speech from text."""

    model_config = ConfigDict(frozen=True)

    clip: AudioClip
    voice: str = ""
    engine: str = ""
    language: str = ""


class SpeechCapabilities(BaseModel):
    """What speech can actually do in the current runtime.

    Built from the live backend and engines, so a caller can render an honest
    capability matrix (and the desktop UI can show why a button is disabled)
    instead of discovering a missing microphone halfway through a run.
    """

    model_config = ConfigDict(frozen=True)

    can_record: bool = False
    can_play: bool = False
    can_transcribe: bool = False
    can_synthesize: bool = False
    backend: str = ""
    backend_available: bool = False
    backend_unavailable_reason: str = ""
    stt_engines: tuple[str, ...] = ()
    tts_engines: tuple[str, ...] = ()
    #: ``True`` when the active engines are cloud (audio would leave the machine).
    cloud_stt: bool = False
    cloud_tts: bool = False
    languages: tuple[str, ...] = ()
    devices: tuple[AudioDevice, ...] = ()
    unavailable_reasons: tuple[str, ...] = ()


class AudioBackend(ABC):
    """The machine's audio hardware seam (record and play).

    Concrete adapters live in :mod:`arabic_agent.speech.backends`. An adapter
    that cannot run here reports ``available = False`` and a reason.

    Both blocking operations accept an optional ``should_cancel`` callable. The
    backend polls it while it holds the device, so a long recording or a playing
    clip can be stopped promptly by the user instead of running to its natural
    end. A backend that cannot poll (or an adapter whose hardware call blocks
    atomically) may ignore it — but it must not *pretend* to support cancellation.
    """

    name: str = "none"
    platform: PlatformKind = PlatformKind.UNKNOWN
    available: bool = False
    unavailable_reason: str = ""

    def require_available(self) -> None:
        """Raise a clear, classified error when the backend cannot run here."""
        if not self.available:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                self.unavailable_reason or "no audio backend is available in this runtime",
                details={"backend": self.name},
            )

    @abstractmethod
    async def list_devices(self) -> list[AudioDevice]:
        """Enumerate input and output endpoints (empty when unavailable)."""

    @abstractmethod
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
        """Capture microphone audio to ``output_path`` and return its metadata."""

    @abstractmethod
    async def play(
        self,
        *,
        clip_path: str,
        device_id: str = "",
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        """Play the audio file at ``clip_path`` through the speaker."""


class TranscriptionEngine(ABC):
    """The speech-to-text seam."""

    name: str = "none"
    available: bool = False
    unavailable_reason: str = ""
    #: Whether using this engine sends audio off the machine.
    is_cloud: bool = False
    languages: tuple[str, ...] = ()

    def require_available(self) -> None:
        if not self.available:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                self.unavailable_reason or "no speech-to-text engine is available",
                details={"engine": self.name},
            )

    @abstractmethod
    async def transcribe(
        self,
        *,
        audio_path: str,
        language: str = "ar",
        prompt: str = "",
    ) -> TranscriptionResult:
        """Transcribe ``audio_path`` to text in ``language``."""


class SynthesisEngine(ABC):
    """The text-to-speech seam."""

    name: str = "none"
    available: bool = False
    unavailable_reason: str = ""
    is_cloud: bool = False
    languages: tuple[str, ...] = ()
    voices: tuple[str, ...] = ()

    def require_available(self) -> None:
        if not self.available:
            from ..core.errors import ConfigurationError

            raise ConfigurationError(
                self.unavailable_reason or "no text-to-speech engine is available",
                details={"engine": self.name},
            )

    @abstractmethod
    async def synthesize(
        self,
        *,
        text: str,
        output_path: str,
        voice: str = "",
        language: str = "ar",
    ) -> SynthesisResult:
        """Synthesize ``text`` to an audio file at ``output_path``."""


__all__ = [
    "FORMAT_FLAC",
    "FORMAT_MP3",
    "FORMAT_OGG",
    "FORMAT_PCM",
    "FORMAT_WAV",
    "KIND_INPUT",
    "KIND_OUTPUT",
    "AudioBackend",
    "AudioClip",
    "AudioDevice",
    "SpeechCapabilities",
    "SpeechEngineError",
    "SynthesisEngine",
    "SynthesisResult",
    "TranscriptionEngine",
    "TranscriptionResult",
    "TranscriptionSegment",
]
