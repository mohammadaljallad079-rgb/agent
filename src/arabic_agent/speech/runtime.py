"""The speech runtime: the single object the speech tools call.

It bundles an :class:`AudioBackend`, a speech-to-text and a text-to-speech
:class:`~arabic_agent.speech.base.TranscriptionEngine` /
:class:`~arabic_agent.speech.base.SynthesisEngine`, the :class:`SpeechPolicy`, a
per-run :class:`SpeechBudget`, the local data boundary, the audit log and a
:class:`SpeechStore`, and enforces the discipline a microphone and speaker
demand:

* every recording and every synthesized clip is a file inside the run's
  workspace — the runtime resolves the path, so a request can never write an
  arbitrary host file;
* a recording's length and a synthesis's text length are bounded by policy;
* every recording and synthesis consumes a per-run budget, so voice cannot
  become a runaway;
* a *cloud* engine is refused unless the runtime was built with cloud speech
  enabled **and** the data boundary allows the audio to leave the machine, so a
  local-only or offline runtime can never leak a recording;
* every action's safe metadata is persisted — kind, duration, language, engine,
  outcome — never the audio bytes, and a transcript only when the caller opted
  into keeping it;
* every action emits an audit event carrying *metadata only*.

It is created once and shared; per-run state is keyed by ``run_id``. Tools reach
it through ``context.metadata["speech_runtime"]``, exactly as the print and
computer tools reach theirs, so the executor and agent runner stay unaware of it.
"""

from __future__ import annotations

import time
from pathlib import Path

from ..audit.base import AuditEventType, AuditLog
from ..core.enums import DataBoundary, DataSensitivity
from ..core.errors import PermissionDeniedError, ValidationError
from ..core.ids import PREFIX_SPEECH, new_id
from ..local.boundary import DataBoundaryPolicy
from .base import (
    AudioBackend,
    AudioClip,
    AudioDevice,
    SpeechCapabilities,
    SynthesisEngine,
    SynthesisResult,
    TranscriptionEngine,
    TranscriptionResult,
)
from .policy import SpeechBudget, SpeechPolicy
from .store import (
    InMemorySpeechStore,
    SpeechActionKind,
    SpeechRecord,
    SpeechStore,
)

#: A recording can contain anything said near the microphone, so it is treated as
#: confidential: the boundary decides whether a remote (cloud) engine may process
#: it, and a secret payload is never sent remotely under any mode.
_SPEECH_SENSITIVITY = DataSensitivity.CONFIDENTIAL


class SpeechRuntime:
    """Executes microphone, transcription, synthesis and playback under governance."""

    def __init__(
        self,
        *,
        backend: AudioBackend,
        transcription: TranscriptionEngine,
        synthesis: SynthesisEngine,
        policy: SpeechPolicy,
        audit: AuditLog,
        boundary: DataBoundaryPolicy | None = None,
        store: SpeechStore | None = None,
        allow_cloud: bool = False,
    ) -> None:
        self._backend = backend
        self._transcription = transcription
        self._synthesis = synthesis
        self._policy = policy
        self._audit = audit
        self._boundary = boundary
        self._store = store or InMemorySpeechStore()
        self._allow_cloud = allow_cloud
        self._budgets: dict[str, SpeechBudget] = {}

    # -- Introspection ---------------------------------------------------
    @property
    def backend(self) -> AudioBackend:
        return self._backend

    @property
    def transcription(self) -> TranscriptionEngine:
        return self._transcription

    @property
    def synthesis(self) -> SynthesisEngine:
        return self._synthesis

    @property
    def policy(self) -> SpeechPolicy:
        return self._policy

    @property
    def store(self) -> SpeechStore:
        return self._store

    @property
    def available(self) -> bool:
        return self._backend.available

    @property
    def unavailable_reason(self) -> str:
        return self._backend.unavailable_reason

    def _budget(self, run_id: str) -> SpeechBudget:
        budget = self._budgets.get(run_id)
        if budget is None:
            budget = SpeechBudget(self._policy)
            self._budgets[run_id] = budget
        return budget

    def capabilities(self) -> SpeechCapabilities:
        """An honest, live capability matrix for the current runtime."""
        reasons: list[str] = []
        if not self._backend.available and self._backend.unavailable_reason:
            reasons.append(self._backend.unavailable_reason)
        if not self._transcription.available and self._transcription.unavailable_reason:
            reasons.append(self._transcription.unavailable_reason)
        if not self._synthesis.available and self._synthesis.unavailable_reason:
            reasons.append(self._synthesis.unavailable_reason)
        stt_engines = (self._transcription.name,) if self._transcription.available else ()
        tts_engines = (self._synthesis.name,) if self._synthesis.available else ()
        languages = tuple(
            dict.fromkeys((*self._transcription.languages, *self._synthesis.languages))
        )
        return SpeechCapabilities(
            can_record=self._backend.available,
            can_play=self._backend.available,
            can_transcribe=self._transcription.available,
            can_synthesize=self._synthesis.available,
            backend=self._backend.name,
            backend_available=self._backend.available,
            backend_unavailable_reason=self._backend.unavailable_reason,
            stt_engines=stt_engines,
            tts_engines=tts_engines,
            cloud_stt=self._transcription.is_cloud,
            cloud_tts=self._synthesis.is_cloud,
            languages=languages,
            unavailable_reasons=tuple(reasons),
        )

    async def list_devices(self, *, tenant_id: str, user_id: str, run_id: str) -> list[AudioDevice]:
        """Enumerate audio endpoints, recording the attempt."""
        devices = await self._backend.list_devices()
        await self._audit.record(
            AuditEventType.SPEECH_DEVICES_LISTED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="audio devices listed",
            data={
                "count": len(devices),
                "backend": self._backend.name,
                "available": self._backend.available,
            },
        )
        return devices

    async def _boundary_check(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        is_cloud: bool,
    ) -> None:
        """Refuse a cloud engine unless cloud speech is enabled and the boundary allows it."""
        if not is_cloud:
            return
        if not self._allow_cloud:
            raise PermissionDeniedError(
                "cloud speech is disabled; audio may not leave this machine",
                details={"reason": "speech_allow_cloud is off"},
            )
        if self._boundary is not None:
            decision = self._boundary.evaluate(
                boundary=DataBoundary.REMOTE,
                sensitivity=_SPEECH_SENSITIVITY,
            )
            await self._audit.record(
                AuditEventType.SPEECH_BOUNDARY_DECISION,
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                message="speech data-boundary decision",
                data={
                    "allowed": decision.allowed,
                    "routed_to": decision.routed_to.value,
                    "reason": decision.reason,
                },
            )
            if not decision.allowed:
                raise PermissionDeniedError(
                    "the data boundary forbids sending audio off this machine",
                    details={"reason": decision.reason},
                )

    # -- Actions ---------------------------------------------------------
    async def record(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        output_path: str,
        device_id: str = "",
        seconds: float = 5.0,
        sample_rate_hz: int = 16000,
        channels: int = 1,
        language: str = "",
    ) -> AudioClip:
        """Capture microphone audio after policy and budget checks."""
        self._backend.require_available()
        if not self._backend.available:
            raise ValidationError("recording is not available in this runtime")
        self._policy.check_recording_seconds(seconds)
        self._budget(run_id).consume_recording(now=time.monotonic())
        clip = await self._backend.record(
            output_path=output_path,
            device_id=device_id,
            seconds=seconds,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        )
        clip = clip.model_copy(update={"language": language or self._policy.default_language})
        await self._store.add(
            SpeechRecord(
                id=new_id(PREFIX_SPEECH),
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=SpeechActionKind.RECORDING,
                engine=clip.engine,
                language=clip.language,
                duration_seconds=clip.duration_seconds,
                size_bytes=clip.size_bytes,
            )
        )
        await self._audit.record(
            AuditEventType.SPEECH_RECORDED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="microphone audio recorded",
            data={
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
                "backend": clip.engine,
                "language": clip.language,
                "device_id": device_id or "default",
            },
        )
        return clip

    async def transcribe(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        audio_path: str,
        language: str = "",
        prompt: str = "",
        keep_text: bool = False,
    ) -> TranscriptionResult:
        """Transcribe an audio file, enforcing the cloud-consent and boundary rules."""
        self._transcription.require_available()
        await self._boundary_check(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            is_cloud=self._transcription.is_cloud,
        )
        size = _file_size(audio_path)
        self._policy.check_audio_size(size)
        language = language or self._policy.default_language
        result = await self._transcription.transcribe(
            audio_path=audio_path, language=language, prompt=prompt
        )
        await self._store.add(
            SpeechRecord(
                id=new_id(PREFIX_SPEECH),
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=SpeechActionKind.TRANSCRIPTION,
                engine=result.engine or self._transcription.name,
                language=result.language or language,
                duration_seconds=result.duration_seconds,
                size_bytes=size,
                # A transcript is only persisted when the caller opted in.
                text=result.text if keep_text else "",
            )
        )
        await self._audit.record(
            AuditEventType.SPEECH_TRANSCRIBED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="audio transcribed",
            data={
                "engine": result.engine or self._transcription.name,
                "language": result.language or language,
                "duration_seconds": result.duration_seconds,
                "size_bytes": size,
                "segments": len(result.segments),
                "kept_text": keep_text,
                "characters": len(result.text),
            },
        )
        return result

    async def synthesize(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        text: str,
        output_path: str,
        voice: str = "",
        language: str = "",
    ) -> SynthesisResult:
        """Synthesize speech from text after policy, budget and boundary checks."""
        self._synthesis.require_available()
        self._policy.check_synthesis_text(text)
        await self._boundary_check(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            is_cloud=self._synthesis.is_cloud,
        )
        self._budget(run_id).consume_synthesis(now=time.monotonic())
        language = language or self._policy.default_language
        result = await self._synthesis.synthesize(
            text=text, output_path=output_path, voice=voice, language=language
        )
        await self._store.add(
            SpeechRecord(
                id=new_id(PREFIX_SPEECH),
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=SpeechActionKind.SYNTHESIS,
                engine=result.engine or self._synthesis.name,
                language=result.language or language,
                duration_seconds=result.clip.duration_seconds,
                size_bytes=result.clip.size_bytes,
            )
        )
        await self._audit.record(
            AuditEventType.SPEECH_SYNTHESIZED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="speech synthesized",
            data={
                "engine": result.engine or self._synthesis.name,
                "voice": result.voice,
                "language": result.language or language,
                "characters": len(text),
                "duration_seconds": result.clip.duration_seconds,
            },
        )
        return result

    async def play(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        clip_path: str,
        device_id: str = "",
    ) -> None:
        """Play an audio file through the speaker, after policy checks."""
        self._backend.require_available()
        size = _file_size(clip_path)
        self._policy.check_audio_size(size)
        await self._backend.play(clip_path=clip_path, device_id=device_id)
        await self._store.add(
            SpeechRecord(
                id=new_id(PREFIX_SPEECH),
                tenant_id=tenant_id,
                user_id=user_id,
                run_id=run_id,
                kind=SpeechActionKind.PLAYBACK,
                engine=self._backend.name,
                size_bytes=size,
            )
        )
        await self._audit.record(
            AuditEventType.SPEECH_PLAYED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message="audio played",
            data={
                "backend": self._backend.name,
                "size_bytes": size,
                "device_id": device_id or "default",
            },
        )

    async def speak(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        text: str,
        output_path: str,
        voice: str = "",
        language: str = "",
        device_id: str = "",
    ) -> SynthesisResult:
        """Synthesize ``text`` and play it — the one-call "answer by voice" path."""
        result = await self.synthesize(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            text=text,
            output_path=output_path,
            voice=voice,
            language=language,
        )
        await self.play(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            clip_path=result.clip.path,
            device_id=device_id,
        )
        return result

    async def listen(
        self,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        output_path: str,
        seconds: float = 5.0,
        device_id: str = "",
        language: str = "",
        keep_text: bool = False,
    ) -> tuple[AudioClip, TranscriptionResult]:
        """Record a spoken command and transcribe it — the "hear a command" path."""
        clip = await self.record(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            output_path=output_path,
            device_id=device_id,
            seconds=seconds,
            language=language,
        )
        result = await self.transcribe(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            audio_path=clip.path,
            language=language,
            keep_text=keep_text,
        )
        return clip, result

    async def list_actions(
        self, *, tenant_id: str, run_id: str | None = None, limit: int = 50
    ) -> list[SpeechRecord]:
        """List persisted speech actions for a run (or the tenant's most recent)."""
        if run_id:
            return await self._store.list_for_run(tenant_id=tenant_id, run_id=run_id)
        return await self._store.list_recent(tenant_id=tenant_id, limit=limit)

    async def get_action(self, *, tenant_id: str, action_id: str) -> SpeechRecord:
        """Return one persisted action, isolated to the tenant."""
        return await self._store.get(action_id, tenant_id=tenant_id)


def _file_size(path: str) -> int:
    try:
        return Path(path).stat().st_size
    except OSError as exc:
        raise ValidationError(f"could not read the audio file: {exc}") from exc


__all__ = ["SpeechRuntime"]
