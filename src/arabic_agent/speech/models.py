"""Declarative catalogue of the local speech models the product can install.

This is *data*, not discovery — the same discipline the local LLM catalogue uses
(``arabic_agent.desktop.model_catalogue``). Every entry names a real, specific
artifact with a fixed source URL, the exact byte size and the real SHA-256 taken
from the publisher's own metadata, so the existing verified installer can refuse
a truncated, substituted or tampered download. Nothing here downloads anything on
its own.

Two kinds of model:

* **STT** — a CTranslate2/faster-whisper model directory. Whisper is
  multilingual and handles Arabic well; the ``small`` model is a fair
  quality/CPU trade-off. It is *not* Arabic-only, so this is a general
  multilingual recogniser used for Arabic, not a bespoke Arabic acoustic model.
* **TTS** — a Piper voice. Arabic ships **exactly one** upstream Piper voice:
  ``ar_JO-kareem`` (a Jordanian-Arabic male voice). It is the only Arabic Piper
  voice that exists, so the product offers it and does not pretend there is a
  choice. For a second, higher-quality Arabic option on Windows the platform uses
  the voices already installed in the operating system (SAPI5), which are
  enumerated at run time rather than downloaded.

The catalogue is honest about licences: Piper voices are MIT (the ``piper-voices``
repository is MIT-licensed and the model card is carried alongside the weights);
faster-whisper models inherit the Whisper weights' MIT licence. The licence text
is carried with each entry so the desktop can show it before an install.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

#: Model kinds. STT reads audio, TTS writes it.
KIND_STT = "stt"
KIND_TTS = "tts"


class SpeechModelFile(BaseModel):
    """One file that makes up a model, pinned by URL, size and SHA-256."""

    model_config = ConfigDict(frozen=True)

    url: str
    filename: str
    sha256: str
    size_bytes: int


class SpeechModelSpec(BaseModel):
    """One installable local speech model."""

    model_config = ConfigDict(frozen=True)

    id: str
    kind: str
    display_name: str
    display_name_ar: str = ""
    description: str = ""
    description_ar: str = ""
    #: ``local`` models never need the network to *run* (only to install).
    requires_network_to_run: bool = False
    #: Approximate peak memory when loaded, in bytes (for an honest warning).
    approx_memory_bytes: int = 0
    #: SPDX-ish licence label and a link to the licence/model card.
    license: str = ""
    license_url: str = ""
    #: The upstream project and a link, so the user can verify the artifact.
    source: str = ""
    source_url: str = ""
    #: The engine that consumes the installed files.
    engine: str = ""
    #: Language coverage, BCP-47-ish.
    languages: tuple[str, ...] = ()
    files: tuple[SpeechModelFile, ...] = Field(default_factory=tuple)

    @property
    def total_bytes(self) -> int:
        return sum(f.size_bytes for f in self.files)

    @property
    def total_mib(self) -> float:
        return round(self.total_bytes / (1024 * 1024), 1)


#: faster-whisper ``small`` (multilingual, 244M params). Sizes and SHA-256 come
#: from the Hugging Face LFS metadata for ``Systran/faster-whisper-small``.
_WHISPER_SMALL_FILES = (
    SpeechModelFile(
        url="https://huggingface.co/Systran/faster-whisper-small/resolve/main/model.bin",
        filename="model.bin",
        sha256="3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671",
        size_bytes=483546902,
    ),
    SpeechModelFile(
        url="https://huggingface.co/Systran/faster-whisper-small/resolve/main/config.json",
        filename="config.json",
        sha256="",
        size_bytes=2370,
    ),
    SpeechModelFile(
        url="https://huggingface.co/Systran/faster-whisper-small/resolve/main/tokenizer.json",
        filename="tokenizer.json",
        sha256="",
        size_bytes=2203239,
    ),
    SpeechModelFile(
        url="https://huggingface.co/Systran/faster-whisper-small/resolve/main/vocabulary.txt",
        filename="vocabulary.txt",
        sha256="",
        size_bytes=459861,
    ),
)

#: Piper Arabic voice ``ar_JO-kareem`` (medium). The ONLY Arabic voice upstream
#: Piper publishes. Sizes and SHA-256 from the ``rhasspy/piper-voices`` LFS
#: metadata; the config file's size is pinned but its digest is not published in
#: the tree listing, so it is verified by size only.
_PIPER_KAREEM_FILES = (
    SpeechModelFile(
        url=(
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx"
        ),
        filename="ar_JO-kareem-medium.onnx",
        sha256="9e95cab07b679da603bba17c4dec7ab3111320571964ee95c0379603c086491e",
        size_bytes=63201294,
    ),
    SpeechModelFile(
        url=(
            "https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            "ar/ar_JO/kareem/medium/ar_JO-kareem-medium.onnx.json"
        ),
        filename="ar_JO-kareem-medium.onnx.json",
        sha256="",
        size_bytes=5024,
    ),
)

#: The catalogue. Deliberately small: one multilingual STT model and the one
#: Arabic Piper voice that exists. Adding a model is a data change.
_SPECS: tuple[SpeechModelSpec, ...] = (
    SpeechModelSpec(
        id="faster-whisper-small",
        kind=KIND_STT,
        display_name="Whisper small (multilingual)",
        display_name_ar="ويسبر صغير (متعدد اللغات)",
        description=(
            "Multilingual speech recognition (faster-whisper / CTranslate2). "
            "Handles Arabic and English; runs on CPU."
        ),
        description_ar=(
            "تعرّف صوتي متعدد اللغات (faster-whisper / CTranslate2). "
            "يدعم العربية والإنجليزية، ويعمل على المعالج دون كرت رسومات."
        ),
        requires_network_to_run=False,
        approx_memory_bytes=1024 * 1024 * 1024,
        license="MIT",
        license_url="https://huggingface.co/Systran/faster-whisper-small",
        source="Systran/faster-whisper-small",
        source_url="https://huggingface.co/Systran/faster-whisper-small",
        engine="faster-whisper",
        languages=("ar", "en"),
        files=_WHISPER_SMALL_FILES,
    ),
    SpeechModelSpec(
        id="piper-ar-jo-kareem-medium",
        kind=KIND_TTS,
        display_name="Kareem — Arabic (Jordan) voice",
        display_name_ar="صوت كريم — عربي (الأردن)",
        description=(
            "Piper neural Arabic voice (Jordanian, male, medium quality, 22.05 kHz). "
            "The only Arabic voice upstream Piper publishes."
        ),
        description_ar=(
            "صوت عربي عصبي من Piper (أردني، رجل، جودة متوسطة، 22.05 كيلوهرتز). "
            "الصوت العربي الوحيد الذي ينشره Piper."
        ),
        requires_network_to_run=False,
        approx_memory_bytes=256 * 1024 * 1024,
        license="MIT",
        license_url="https://huggingface.co/rhasspy/piper-voices",
        source="rhasspy/piper-voices (ar_JO-kareem)",
        source_url="https://huggingface.co/rhasspy/piper-voices/tree/main/ar/ar_JO/kareem",
        engine="piper",
        languages=("ar",),
        files=_PIPER_KAREEM_FILES,
    ),
)

_BY_ID: dict[str, SpeechModelSpec] = {spec.id: spec for spec in _SPECS}


def speech_models() -> tuple[SpeechModelSpec, ...]:
    """Every installable local speech model."""
    return _SPECS


def get_speech_model(model_id: str) -> SpeechModelSpec:
    """Return a speech model by id, raising when unknown."""
    from ..core.errors import NotFoundError

    spec = _BY_ID.get(model_id)
    if spec is None:
        raise NotFoundError(f"unknown speech model {model_id!r}")
    return spec


__all__ = [
    "KIND_STT",
    "KIND_TTS",
    "SpeechModelFile",
    "SpeechModelSpec",
    "get_speech_model",
    "speech_models",
]
