"""Windows SAPI5 text-to-speech engine.

Piper ships exactly one Arabic voice; Windows itself often ships more, because
the operating system's SAPI5 stack exposes every voice the user (or an Arabic
language pack) has installed. This engine drives that stack, so the product can
speak Arabic through a real, licensed system voice *without downloading a model*
and without adding a Python dependency.

It is the same two-layer shape as the audio backends and the print backend:

* :class:`SapiVoiceAdapter` is the only place that touches SAPI. It is a narrow,
  injectable seam returning plain values, so a test can substitute a fake adapter
  and exercise the whole engine off Windows.
* :class:`WindowsSapiSynthesizer` maps those values onto the shared models and
  reports availability honestly.

The real adapter drives SAPI through **PowerShell** (``System.Speech``), which is
present on every supported Windows version. The synthesis text is written to a
temporary UTF-8 file and read by the script, so the text is never interpolated
into a command line — a spoken sentence can never be interpreted as a flag or a
command, and Arabic text is not mangled by console code pages. The temporary file
is created next to the output file (inside the sandbox) and removed afterwards.

On non-Windows hosts the adapter reports itself unavailable with a clear reason.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from ...core.enums import PlatformKind
from ...core.errors import ExternalServiceError
from ..base import (
    FORMAT_WAV,
    AudioClip,
    SpeechEngineError,
    SynthesisEngine,
    SynthesisResult,
)

logger = logging.getLogger(__name__)

#: SAPI ``Rate`` is in the range -10..10; the character's ``rate`` (≈1.0) maps to
#: the middle of that range.
_RATE_MIN = -10
_RATE_MAX = 10

#: PowerShell script: print installed voices as ``name<TAB>culture`` lines.
_LIST_VOICES_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
foreach ($v in $synth.GetInstalledVoices()) {
  $i = $v.VoiceInfo
  Write-Output ($i.Name + [char]9 + $i.Culture.Name)
}
"""

#: PowerShell script: synthesize the text in ``$env:AAP_SPEECH_TEXT_FILE`` to the
#: WAV file in ``$env:AAP_SPEECH_OUT_FILE`` using the optional voice in
#: ``$env:AAP_SPEECH_VOICE`` and rate in ``$env:AAP_SPEECH_RATE``.
_SYNTH_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:AAP_SPEECH_VOICE) { $synth.SelectVoice($env:AAP_SPEECH_VOICE) }
if ($env:AAP_SPEECH_RATE) { $synth.Rate = [int]$env:AAP_SPEECH_RATE }
$synth.SetOutputToWaveFile($env:AAP_SPEECH_OUT_FILE)
$text = [System.IO.File]::ReadAllText($env:AAP_SPEECH_TEXT_FILE, [System.Text.Encoding]::UTF8)
$synth.Speak($text)
$synth.Dispose()
"""


class SapiVoiceAdapter(ABC):
    """The narrow seam over Windows SAPI5."""

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return ``(available, reason_if_not)``."""

    @abstractmethod
    def list_voices(self) -> list[dict[str, str]]:
        """Return records with ``name`` and ``culture`` keys."""

    @abstractmethod
    def synthesize(self, *, text: str, output_path: str, voice: str = "", rate: int = 0) -> None:
        """Synthesize ``text`` to a WAV file at ``output_path``."""


class PowerShellSapiAdapter(SapiVoiceAdapter):
    """The real adapter, driving SAPI5 through PowerShell ``System.Speech``."""

    def __init__(self) -> None:
        self._powershell = _find_powershell()

    def available(self) -> tuple[bool, str]:
        if os.name != "nt":  # pragma: no cover - platform dependent
            return (False, "the Windows SAPI engine is only available on Windows")
        if self._powershell is None:
            return (False, "PowerShell (powershell.exe) was not found on this machine")
        return (True, "")

    def list_voices(self) -> list[dict[str, str]]:  # pragma: no cover - Windows only
        if self._powershell is None or os.name != "nt":
            return []
        output = _run_powershell(self._powershell, _LIST_VOICES_SCRIPT)
        voices: list[dict[str, str]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line or "\t" not in line:
                continue
            name, _, culture = line.partition("\t")
            voices.append({"name": name.strip(), "culture": culture.strip()})
        return voices

    def synthesize(
        self, *, text: str, output_path: str, voice: str = "", rate: int = 0
    ) -> None:  # pragma: no cover - Windows only
        if self._powershell is None:
            raise SpeechEngineError("PowerShell was not found; Windows SAPI is unavailable")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # The text is passed through a UTF-8 file, never a command line.
        text_fd, text_path = tempfile.mkstemp(
            suffix=".txt", prefix="aap-speech-", dir=str(Path(output_path).parent)
        )
        try:
            with os.fdopen(text_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            env = dict(os.environ)
            env["AAP_SPEECH_TEXT_FILE"] = text_path
            env["AAP_SPEECH_OUT_FILE"] = output_path
            env["AAP_SPEECH_VOICE"] = voice
            env["AAP_SPEECH_RATE"] = str(max(_RATE_MIN, min(_RATE_MAX, rate)))
            _run_powershell(self._powershell, _SYNTH_SCRIPT, env=env)
        finally:
            try:
                os.unlink(text_path)
            except OSError:
                logger.debug("could not remove temporary speech text file", exc_info=True)


def _find_powershell() -> str | None:
    import shutil

    for candidate in ("powershell.exe", "powershell", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_powershell(
    powershell: str, script: str, *, env: dict[str, str] | None = None
) -> str:  # pragma: no cover - Windows only
    argv = [
        powershell,
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - fixed program, script is a constant
            argv, capture_output=True, timeout=120, check=False, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ExternalServiceError(
            "the Windows speech engine could not run", details={"error": str(exc)}
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="ignore").strip()
        raise ExternalServiceError(
            "the Windows speech engine failed",
            details={"returncode": result.returncode, "stderr": stderr[:500]},
        )
    return result.stdout.decode("utf-8", errors="ignore")


class WindowsSapiSynthesizer(SynthesisEngine):
    """Text-to-speech through the voices already installed in Windows."""

    name = "windows-sapi"
    is_cloud = False

    def __init__(self, *, adapter: SapiVoiceAdapter | None = None) -> None:
        self._adapter = adapter or PowerShellSapiAdapter()
        available, reason = self._adapter.available()
        self._voices: list[dict[str, str]] = self._adapter.list_voices() if available else []
        self.available = available and bool(self._voices)
        if not available:
            self.unavailable_reason = reason
        elif not self._voices:
            self.unavailable_reason = "no SAPI voices are installed on this machine"
        #: Voices as ``name`` strings, for the capability report.
        self.voices = tuple(v.get("name", "") for v in self._voices)
        #: Languages covered by the installed voices (from their culture).
        cultures = {v.get("culture", "") for v in self._voices}
        languages: list[str] = []
        if any(c.lower().startswith("ar") for c in cultures):
            languages.append("ar")
        if any(c.lower().startswith("en") for c in cultures):
            languages.append("en")
        self.languages = tuple(languages)

    def arabic_voices(self) -> tuple[str, ...]:
        """Names of installed voices whose culture is Arabic."""
        return tuple(
            v.get("name", "") for v in self._voices if v.get("culture", "").lower().startswith("ar")
        )

    def _pick_voice(self, requested: str, language: str) -> str:
        """Choose a voice: the requested one, else an Arabic one, else the first."""
        if requested:
            for v in self._voices:
                if requested.lower() in v.get("name", "").lower():
                    return v.get("name", "")
        if language == "ar":
            arabic = self.arabic_voices()
            if arabic:
                return arabic[0]
        return self._voices[0].get("name", "") if self._voices else ""

    def _synthesize_sync(
        self, text: str, output_path: str, voice: str, language: str, rate: int
    ) -> None:
        chosen = self._pick_voice(voice, language)
        try:
            self._adapter.synthesize(text=text, output_path=output_path, voice=chosen, rate=rate)
        except ExternalServiceError as exc:
            raise SpeechEngineError(
                "Windows speech synthesis failed", details={"engine": self.name, "error": str(exc)}
            ) from exc

    async def synthesize(
        self, *, text: str, output_path: str, voice: str = "", language: str = "ar"
    ) -> SynthesisResult:
        self.require_available()
        await asyncio.to_thread(self._synthesize_sync, text, output_path, voice, language, 0)
        chosen = self._pick_voice(voice, language)
        clip = _wav_clip(output_path, engine=self.name)
        return SynthesisResult(
            clip=clip,
            voice=chosen,
            engine=self.name,
            language=language,
        )


def _wav_clip(path: str, *, engine: str) -> AudioClip:
    import wave

    from ...core.ids import PREFIX_AUDIO, new_id

    duration = 0.0
    size = 0
    sample_rate = 0
    try:
        size = os.path.getsize(path)
        with wave.open(path, "rb") as handle:
            sample_rate = handle.getframerate()
            frames = handle.getnframes()
            duration = frames / sample_rate if sample_rate else 0.0
    except (OSError, wave.Error):
        logger.debug("could not read SAPI WAV header for %s", path, exc_info=True)
    return AudioClip(
        id=new_id(PREFIX_AUDIO),
        path=path,
        format=FORMAT_WAV,
        sample_rate_hz=sample_rate,
        duration_seconds=duration,
        size_bytes=size,
        engine=engine,
    )


#: The platform this engine targets, exported for the factory.
PLATFORM = PlatformKind.WINDOWS

__all__ = [
    "PLATFORM",
    "PowerShellSapiAdapter",
    "SapiVoiceAdapter",
    "WindowsSapiSynthesizer",
]
