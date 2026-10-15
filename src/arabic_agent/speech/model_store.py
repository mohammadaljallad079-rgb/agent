"""Installation and resolution of local speech models.

A thin product layer over the *existing* verified installer
(:func:`arabic_agent.local.model_install.install_model`): it downloads each file
of a :class:`~arabic_agent.speech.models.SpeechModelSpec` into a per-model
directory under the speech store, verifying size and SHA-256 exactly as the LLM
installer does. Nothing here invents a download — an install happens only when a
caller explicitly asks for a named catalogue model.

Two responsibilities:

* :meth:`SpeechModelStore.install` — fetch and verify the files of one model.
* :meth:`SpeechModelStore.resolve` — given a model id, return the concrete
  artifact a backend needs: the CTranslate2 model *directory* for faster-whisper,
  or the Piper ``.onnx`` voice *file*.

The store is deliberately conservative: it never treats "the files exist" as
"the model works" (loading is a separate, real step), and it reports a partial
install honestly rather than a boolean success.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..core.errors import NotFoundError, ValidationError
from ..local.model_install import install_model
from ..local.model_security import resolve_storage_dir
from .models import KIND_STT, KIND_TTS, get_speech_model

logger = logging.getLogger(__name__)

#: Sub-directory of the model storage root that holds speech models.
SPEECH_SUBDIR = "speech"


@dataclass
class FileProgress:
    """Progress of one file within an install (safe to serialise)."""

    filename: str
    written: int = 0
    total: int | None = None


@dataclass
class SpeechInstallResult:
    """Outcome of installing one speech model."""

    model_id: str
    success: bool
    detail: str = ""
    path: str = ""
    files: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "model_id": self.model_id,
            "success": self.success,
            "detail": self.detail,
            "path": self.path,
            "files": list(self.files),
        }


class SpeechModelStore:
    """Installs and resolves local speech models under the model storage root."""

    def __init__(self, *, storage_dir: str) -> None:
        self._root = resolve_storage_dir(storage_dir) / SPEECH_SUBDIR

    @property
    def root(self) -> Path:
        return self._root

    def model_dir(self, model_id: str) -> Path:
        """The directory a model's files live in (created on install)."""
        spec = get_speech_model(model_id)
        return self._root / spec.id

    def is_installed(self, model_id: str) -> bool:
        """Whether every file of the model is present (existence, not validity)."""
        spec = get_speech_model(model_id)
        directory = self.model_dir(model_id)
        return all((directory / f.filename).is_file() for f in spec.files)

    def resolve(self, model_id: str) -> str:
        """Return the artifact path a backend needs for ``model_id``.

        * STT: the model *directory* (faster-whisper loads a CTranslate2 folder).
        * TTS: the ``.onnx`` voice *file* (Piper loads a single voice file).
        """
        spec = get_speech_model(model_id)
        directory = self.model_dir(model_id)
        if not self.is_installed(model_id):
            raise NotFoundError(
                f"speech model {model_id!r} is not installed",
                details={"model_id": model_id},
            )
        if spec.kind == KIND_TTS:
            onnx = next((f.filename for f in spec.files if f.filename.endswith(".onnx")), None)
            if onnx is None:
                raise ValidationError(f"speech model {model_id!r} has no voice file")
            return str(directory / onnx)
        return str(directory)

    async def install(
        self,
        model_id: str,
        *,
        on_progress: Callable[[FileProgress], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> SpeechInstallResult:
        """Download and verify every file of ``model_id``.

        Cancellation stops between files and reports a partial install honestly;
        a single file's download is itself resumable through the underlying
        installer, so a later call continues rather than restarting.
        """
        spec = get_speech_model(model_id)
        directory = self.model_dir(model_id)
        directory.mkdir(parents=True, exist_ok=True)
        installed: list[str] = []
        for file in spec.files:
            if should_cancel is not None and should_cancel():
                return SpeechInstallResult(
                    model_id=model_id,
                    success=False,
                    path=str(directory),
                    files=installed,
                    detail="cancelled before completion; partial files kept for resume",
                )

            def _progress(written: int, total: int | None, *, _name: str = file.filename) -> None:
                if on_progress is not None:
                    on_progress(FileProgress(filename=_name, written=written, total=total))

            result = await install_model(
                model_id=f"{spec.id}/{file.filename}",
                url=file.url,
                storage_dir=str(directory),
                filename=file.filename,
                expected_sha256=file.sha256 or None,
                expected_size=file.size_bytes or None,
                should_cancel=should_cancel,
                on_progress=_progress,
            )
            if not result.success:
                return SpeechInstallResult(
                    model_id=model_id,
                    success=False,
                    path=str(directory),
                    files=installed,
                    detail=f"{file.filename}: {result.detail}",
                )
            installed.append(file.filename)
        logger.info("installed speech model %s (%d files)", model_id, len(installed))
        return SpeechInstallResult(
            model_id=model_id,
            success=True,
            path=str(directory),
            files=installed,
            detail="installed and verified",
        )

    def list_installed(self) -> list[str]:
        """Ids of the catalogue models whose files are all present."""
        from .models import speech_models

        return [spec.id for spec in speech_models() if self.is_installed(spec.id)]


def stt_model_dir(storage_dir: str, model_id: str) -> str | None:
    """The faster-whisper model directory for ``model_id``, or ``None``."""
    try:
        store = SpeechModelStore(storage_dir=storage_dir)
        if get_speech_model(model_id).kind != KIND_STT:
            return None
        return store.resolve(model_id)
    except NotFoundError:
        return None


def tts_voice_file(storage_dir: str, model_id: str) -> str | None:
    """The Piper voice file for ``model_id``, or ``None``."""
    try:
        store = SpeechModelStore(storage_dir=storage_dir)
        if get_speech_model(model_id).kind != KIND_TTS:
            return None
        return store.resolve(model_id)
    except NotFoundError:
        return None


__all__ = [
    "SPEECH_SUBDIR",
    "FileProgress",
    "SpeechInstallResult",
    "SpeechModelStore",
    "stt_model_dir",
    "tts_voice_file",
]
