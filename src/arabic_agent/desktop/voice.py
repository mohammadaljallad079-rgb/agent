"""The desktop voice assistant: a real push-to-talk loop in the product.

This is the user-facing surface the "SUPER AI AGENT" desktop exposes for voice.
It is deliberately *not* a new agent: a spoken turn is a normal durable run
started through :class:`~arabic_agent.api.service.RunService`, executed by the
same agent core, under the same permissions, approvals and audit. What this
service adds is only the audio plumbing and the two governance questions a voice
button must ask before it touches hardware:

* **Is speech enabled?** — ``settings.speech_enabled`` (off by default).
* **Is the operation permitted?** — ``MICROPHONE_RECORD`` and ``SPEAKER_PLAYBACK``
  are evaluated by the *same* :class:`~arabic_agent.permissions.policy.PolicyEngine`
  the agent uses. Under the platform default policy both require approval, so the
  service refuses with a clear, structured reason instead of silently recording;
  the approval is a real record the user resolves through the existing approvals
  API. The service never self-approves.

Audio files are always written inside the run's own workspace (under a ``voice/``
sub-directory), so a voice turn can never write an arbitrary host file — the same
boundary the speech tools honour.

The turn has two honest halves: :meth:`transcribe` records and recognises a
command, and :meth:`speak` synthesizes and plays a reply. Neither half fabricates
a capability: when the microphone, the recogniser or the voice is missing, the
capability report says so and the call fails with the real reason.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..audit.base import AuditEventType
from ..container import Platform
from ..core.errors import ConfigurationError, PermissionDeniedError, ValidationError
from ..core.ids import PREFIX_RUN, new_id
from ..permissions.base import Permission, Principal, default_permission_risk
from ..speech.cancel import CancelledError, CancelToken
from ..speech.character import SUPER_AI_CHARACTER
from ..speech.model_store import SpeechModelStore
from ..speech.runtime import SpeechRuntime

logger = logging.getLogger(__name__)

#: Operation kinds that can be cancelled while in flight.
OP_RECORD = "record"
OP_PLAY = "play"


@dataclass(frozen=True)
class VoicePrincipal:
    """The single local principal the desktop voice surface operates as."""

    tenant_id: str = "local"
    user_id: str = "local"

    def to_principal(self) -> Principal:
        return Principal(tenant_id=self.tenant_id, user_id=self.user_id, roles=frozenset({"owner"}))


class DesktopVoiceService:
    """Product-level voice operations over the platform's speech runtime."""

    def __init__(
        self,
        *,
        platform: Platform,
        principal: Principal | None = None,
    ) -> None:
        self._platform = platform
        self._principal = principal or VoicePrincipal().to_principal()
        self._models = SpeechModelStore(
            storage_dir=platform.settings.local_model_storage_dir
            or str(Path(platform.settings.local_store_dir) / "models")
        )
        #: One cancellation token per in-flight operation kind, so the UI's stop
        #: button can interrupt a recording or a playback that is already running.
        self._cancels: dict[str, CancelToken] = {}

    # -- Cancellation ----------------------------------------------------
    def _begin(self, op: str) -> CancelToken:
        """Start an operation, replacing (and stopping) any previous one of its kind."""
        previous = self._cancels.get(op)
        if previous is not None and not previous.cancelled:
            previous.cancel("superseded by a new request")
        token = CancelToken()
        self._cancels[op] = token
        return token

    def _end(self, op: str, token: CancelToken) -> None:
        if self._cancels.get(op) is token:
            self._cancels.pop(op, None)

    def _cancel(self, op: str) -> bool:
        token = self._cancels.get(op)
        if token is None or token.cancelled:
            return False
        token.cancel(f"the user stopped {op}")
        return True

    def stop(self, *, op: str | None = None) -> dict[str, object]:
        """Stop an in-flight recording and/or playback.

        Returns which operations were actually cancelled so the UI can say
        "stopped" truthfully instead of claiming a stop that had nothing to stop.
        """
        stopped: list[str] = []
        for kind in (OP_RECORD, OP_PLAY) if op is None else (op,):
            if self._cancel(kind):
                stopped.append(kind)
        return {"stopped": stopped, "cancelled": bool(stopped)}

    @property
    def active(self) -> dict[str, bool]:
        """Whether a recording / playback is currently in flight."""
        return {
            OP_RECORD: OP_RECORD in self._cancels,
            OP_PLAY: OP_PLAY in self._cancels,
        }

    @property
    def _runtime(self) -> SpeechRuntime | None:
        return self._platform.speech_runtime

    # -- Capabilities / governance --------------------------------------
    def _decision(self, permission: Permission) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a speech permission, honestly."""
        from ..core.enums import PermissionEffect

        decision = self._platform.policy.evaluate(
            self._principal,
            tool_name="voice",
            permission=permission,
            risk=default_permission_risk(permission),
        )
        return decision.effect is PermissionEffect.ALLOW, decision.reason

    def capabilities(self) -> dict[str, object]:
        """What voice can actually do here, and what it needs to do more."""
        settings = self._platform.settings
        runtime = self._runtime
        enabled = bool(settings.speech_enabled)
        base: dict[str, object] = {
            "enabled": enabled,
            "character": SUPER_AI_CHARACTER.to_public_dict(),
            "installed_models": self._models.list_installed(),
        }
        if runtime is None:
            base.update(
                {
                    "available": False,
                    "unavailable_reason": (
                        "voice is disabled in settings"
                        if not enabled
                        else "the speech runtime is not available in this build"
                    ),
                    "can_record": False,
                    "can_play": False,
                    "can_transcribe": False,
                    "can_synthesize": False,
                    "stt_engine": "",
                    "tts_engine": "",
                    "languages": [],
                    "cloud_stt": False,
                    "cloud_tts": False,
                    "record_allowed": False,
                    "record_reason": "",
                    "playback_allowed": False,
                    "playback_reason": "",
                    "devices": [],
                    "recording": False,
                    "playing": False,
                    "voice": SUPER_AI_CHARACTER.voice.preferred_voice,
                }
            )
            return base
        caps = runtime.capabilities()
        record_allowed, record_reason = self._decision(Permission.MICROPHONE_RECORD)
        play_allowed, play_reason = self._decision(Permission.SPEAKER_PLAYBACK)
        active = self.active
        base.update(
            {
                "available": bool(caps.can_record or caps.can_play or caps.can_synthesize),
                "unavailable_reason": "; ".join(caps.unavailable_reasons),
                "can_record": caps.can_record,
                "can_play": caps.can_play,
                "can_transcribe": caps.can_transcribe,
                "can_synthesize": caps.can_synthesize,
                "stt_engine": runtime.transcription.name if caps.can_transcribe else "",
                "tts_engine": runtime.synthesis.name if caps.can_synthesize else "",
                "languages": list(caps.languages),
                "cloud_stt": caps.cloud_stt,
                "cloud_tts": caps.cloud_tts,
                "record_allowed": record_allowed,
                "record_reason": record_reason,
                "playback_allowed": play_allowed,
                "playback_reason": play_reason,
                "devices": [_device_dict(d) for d in caps.devices],
                "recording": active[OP_RECORD],
                "playing": active[OP_PLAY],
                "voice": SUPER_AI_CHARACTER.voice.preferred_voice,
            }
        )
        return base

    async def list_devices(self) -> dict[str, object]:
        runtime = self._runtime
        if runtime is None:
            return {"devices": []}
        devices = await runtime.list_devices(
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="desktop-voice",
        )
        return {"devices": [_device_dict(d) for d in devices]}

    # -- Speech model management ----------------------------------------
    def list_models(self) -> dict[str, object]:
        """The installable speech catalogue with real installed state."""
        from ..speech.models import speech_models

        installed = set(self._models.list_installed())
        return {
            "models": [
                {
                    "id": spec.id,
                    "kind": spec.kind,
                    "display_name": spec.display_name,
                    "display_name_ar": spec.display_name_ar,
                    "description": spec.description,
                    "description_ar": spec.description_ar,
                    "size_bytes": spec.total_bytes,
                    "size_mib": spec.total_mib,
                    "approx_memory_bytes": spec.approx_memory_bytes,
                    "license": spec.license,
                    "license_url": spec.license_url,
                    "source": spec.source,
                    "source_url": spec.source_url,
                    "engine": spec.engine,
                    "languages": list(spec.languages),
                    "installed": spec.id in installed,
                }
                for spec in speech_models()
            ]
        }

    async def install_model(self, model_id: str) -> dict[str, object]:
        """Install a speech model from the catalogue (verified download)."""
        if not self._platform.settings.speech_enabled:
            raise ConfigurationError("voice is disabled; enable it before installing a voice model")
        result = await self._models.install(model_id)
        await self._platform.audit.record(
            AuditEventType.MODEL_ACTIVATED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="voice",
            message="speech model install attempted",
            data={"model_id": model_id, "success": result.success, "detail": result.detail},
        )
        return result.as_dict()

    def remove_model(self, model_id: str) -> dict[str, object]:
        """Remove an installed speech model's files from the speech store."""
        from ..local.model_install import read_manifest, write_manifest
        from ..speech.models import get_speech_model

        spec = get_speech_model(model_id)
        directory = self._models.model_dir(model_id)
        removed: list[str] = []
        for file in spec.files:
            target = directory / file.filename
            if target.is_file():
                target.unlink()
                removed.append(file.filename)
        manifest = read_manifest(directory)
        for file in spec.files:
            manifest.pop(f"{spec.id}/{file.filename}", None)
        write_manifest(directory, manifest)
        try:
            directory.rmdir()
        except OSError:
            logger.debug("speech model directory not empty after removal: %s", directory)
        return {"model_id": model_id, "removed": removed}

    # -- The voice turn --------------------------------------------------
    async def start_voice_run(
        self, *, goal: str, workspace_dir: str | None = None
    ) -> dict[str, object]:
        """Start a durable run for a spoken command, as the voice specialist."""
        from ..api.service import RunService

        if not goal or not goal.strip():
            raise ValidationError("a spoken command is required")
        service = RunService(self._platform)
        record = await service.start(
            principal=self._principal,
            goal=goal.strip(),
            workspace_dir=workspace_dir,
            specialist="voice",
        )
        return {
            "run_id": record.id,
            "workspace_dir": record.workspace_dir,
            "goal": record.goal,
            "status": record.status.value,
        }

    async def _voice_dir(self, run_id: str) -> Path:
        """The run's workspace ``voice/`` directory, created on demand."""
        from ..api.service import RunService

        service = RunService(self._platform)
        record = await service.get(run_id, tenant_id=self._principal.tenant_id)
        directory = Path(record.workspace_dir) / self._platform.settings.speech_workspace_subdir
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def _scratch_dir(self) -> Path:
        """A voice directory for a capture with no run (used before a run exists).

        Lives under the local store, never an arbitrary host path, so the same
        "audio stays inside the sandbox" rule holds for a preview recording.
        """
        directory = (
            Path(self._platform.settings.local_store_dir)
            / self._platform.settings.speech_workspace_subdir
        )
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    async def _audio_dir(self, run_id: str | None) -> Path:
        return await self._voice_dir(run_id) if run_id else self._scratch_dir()

    async def transcribe(
        self,
        *,
        run_id: str | None = None,
        seconds: float = 5.0,
        device_id: str = "",
        language: str = "ar",
        keep_text: bool = True,
    ) -> dict[str, object]:
        """Record a spoken command and return its text — the "listen" half.

        Requires ``MICROPHONE_RECORD`` to be allowed; under the default policy it
        requires approval, so this refuses with a structured reason rather than
        recording ambient audio the user did not authorise.

        The recording is cancellable: a stop from the UI ends it promptly and the
        result carries ``cancelled: True`` instead of raising, because a
        deliberate stop is not a failure. Any audio captured before the stop is
        still transcribed, so a clipped command is not thrown away.
        """
        runtime = self._runtime
        if runtime is None:
            raise ConfigurationError("voice is not available in this runtime")
        allowed, reason = self._decision(Permission.MICROPHONE_RECORD)
        if not allowed:
            raise PermissionDeniedError(
                reason or "microphone recording is not permitted",
                details={"permission": Permission.MICROPHONE_RECORD.value},
            )
        directory = await self._audio_dir(run_id)
        output = str(directory / f"cmd-{new_id(PREFIX_RUN)}.wav")
        token = self._begin(OP_RECORD)
        try:
            clip, result = await runtime.listen(
                tenant_id=self._principal.tenant_id,
                user_id=self._principal.user_id,
                run_id=run_id or "voice-preview",
                output_path=output,
                seconds=seconds,
                device_id=device_id,
                language=language,
                keep_text=keep_text,
                should_cancel=token.should_cancel,
            )
        except CancelledError:
            return {
                "cancelled": True,
                "text": "",
                "language": language,
                "confidence": None,
                "duration_seconds": 0.0,
                "engine": runtime.transcription.name,
                "audio_path": "",
                "segments": [],
            }
        finally:
            self._end(OP_RECORD, token)
        return {
            "cancelled": False,
            "text": result.text,
            "language": result.language or language,
            "confidence": result.confidence,
            "duration_seconds": result.duration_seconds,
            "engine": result.engine,
            "audio_path": clip.path,
            "segments": [
                {"start": s.start_seconds, "end": s.end_seconds, "text": s.text}
                for s in result.segments
            ],
        }

    async def synthesize(
        self,
        *,
        run_id: str | None = None,
        text: str,
        voice: str = "",
        language: str = "ar",
    ) -> dict[str, object]:
        """Synthesize a reply to an audio file without playing it.

        The counterpart to :meth:`speak` for a caller that wants the audio (a
        preview, a download, or a "read this aloud later" action). Gated by
        ``SPEAKER_PLAYBACK`` because it produces the agent's voice.
        """
        runtime = self._runtime
        if runtime is None:
            raise ConfigurationError("voice is not available in this runtime")
        allowed, reason = self._decision(Permission.SPEAKER_PLAYBACK)
        if not allowed:
            raise PermissionDeniedError(
                reason or "speech synthesis is not permitted",
                details={"permission": Permission.SPEAKER_PLAYBACK.value},
            )
        if not text or not text.strip():
            raise ValidationError("there is nothing to say")
        directory = await self._audio_dir(run_id)
        output = str(directory / f"reply-{new_id(PREFIX_RUN)}.wav")
        result = await runtime.synthesize(
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id=run_id or "voice-preview",
            text=text,
            output_path=output,
            voice=voice or SUPER_AI_CHARACTER.voice.preferred_voice,
            language=language,
        )
        return {
            "cancelled": False,
            "voice": result.voice,
            "engine": result.engine,
            "language": result.language,
            "audio_path": result.clip.path,
            "duration_seconds": result.clip.duration_seconds,
        }

    async def speak(
        self,
        *,
        run_id: str | None = None,
        text: str,
        voice: str = "",
        language: str = "ar",
        device_id: str = "",
    ) -> dict[str, object]:
        """Synthesize and play a reply — the "answer by voice" half.

        Requires ``SPEAKER_PLAYBACK`` to be allowed (approval-gated by default).
        Playback is cancellable: a stop ends it promptly and the result carries
        ``cancelled: True`` (the synthesized audio is still on disk).
        """
        runtime = self._runtime
        if runtime is None:
            raise ConfigurationError("voice is not available in this runtime")
        allowed, reason = self._decision(Permission.SPEAKER_PLAYBACK)
        if not allowed:
            raise PermissionDeniedError(
                reason or "speaker playback is not permitted",
                details={"permission": Permission.SPEAKER_PLAYBACK.value},
            )
        if not text or not text.strip():
            raise ValidationError("there is nothing to say")
        directory = await self._audio_dir(run_id)
        output = str(directory / f"reply-{new_id(PREFIX_RUN)}.wav")
        synthesized = await runtime.synthesize(
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id=run_id or "voice-preview",
            text=text,
            output_path=output,
            voice=voice or SUPER_AI_CHARACTER.voice.preferred_voice,
            language=language,
        )
        token = self._begin(OP_PLAY)
        try:
            await runtime.play(
                tenant_id=self._principal.tenant_id,
                user_id=self._principal.user_id,
                run_id=run_id or "voice-preview",
                clip_path=synthesized.clip.path,
                device_id=device_id,
                language=language,
                should_cancel=token.should_cancel,
            )
        except CancelledError:
            return {
                "cancelled": True,
                "voice": synthesized.voice,
                "engine": synthesized.engine,
                "language": synthesized.language,
                "audio_path": synthesized.clip.path,
                "duration_seconds": synthesized.clip.duration_seconds,
            }
        finally:
            self._end(OP_PLAY, token)
        return {
            "cancelled": False,
            "voice": synthesized.voice,
            "engine": synthesized.engine,
            "language": synthesized.language,
            "audio_path": synthesized.clip.path,
            "duration_seconds": synthesized.clip.duration_seconds,
        }

    async def play_file(
        self,
        *,
        run_id: str | None = None,
        path: str,
        device_id: str = "",
        language: str = "ar",
    ) -> dict[str, object]:
        """Replay a clip that already lives in the voice workspace.

        The path is resolved inside the run's (or the scratch) voice directory so
        a caller can never ask the speaker to play an arbitrary host file.
        Cancellable like :meth:`speak`.
        """
        runtime = self._runtime
        if runtime is None:
            raise ConfigurationError("voice is not available in this runtime")
        allowed, reason = self._decision(Permission.SPEAKER_PLAYBACK)
        if not allowed:
            raise PermissionDeniedError(
                reason or "speaker playback is not permitted",
                details={"permission": Permission.SPEAKER_PLAYBACK.value},
            )
        directory = await self._audio_dir(run_id)
        clip_path = _resolve_voice_clip(directory, path)
        token = self._begin(OP_PLAY)
        try:
            await runtime.play(
                tenant_id=self._principal.tenant_id,
                user_id=self._principal.user_id,
                run_id=run_id or "voice-preview",
                clip_path=str(clip_path),
                device_id=device_id,
                language=language,
                should_cancel=token.should_cancel,
            )
        except CancelledError:
            return {"cancelled": True, "audio_path": str(clip_path)}
        finally:
            self._end(OP_PLAY, token)
        return {"cancelled": False, "audio_path": str(clip_path)}

    async def list_actions(
        self, *, run_id: str | None = None, limit: int = 50
    ) -> dict[str, object]:
        """Persisted voice actions (metadata only), newest first."""
        runtime = self._runtime
        if runtime is None:
            return {"enabled": False, "actions": [], "count": 0}
        records = await runtime.list_actions(
            tenant_id=self._principal.tenant_id, run_id=run_id, limit=limit
        )
        return {
            "enabled": True,
            "actions": [
                {
                    "id": r.id,
                    "kind": r.kind.value,
                    "state": r.state.value,
                    "engine": r.engine,
                    "language": r.language,
                    "duration_seconds": r.duration_seconds,
                    "size_bytes": r.size_bytes,
                    "run_id": r.run_id,
                    "text": r.text,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in records
            ],
            "count": len(records),
        }


def _device_dict(d: object) -> dict[str, object]:
    from ..speech.base import AudioDevice

    assert isinstance(d, AudioDevice)
    return {
        "id": d.id,
        "name": d.name,
        "kind": d.kind,
        "default": d.is_default,
        "available": d.available,
        "unavailable_reason": d.unavailable_reason,
    }


def _resolve_voice_clip(directory: Path, path: str) -> Path:
    """Resolve a requested clip to a file inside ``directory`` only.

    A bare filename is joined to the directory; an absolute or relative path is
    resolved and then checked to be inside it. This is what stops a caller from
    asking the speaker to play an arbitrary host file (``../../etc/passwd``).
    """
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else directory / candidate).resolve()
    root = directory.resolve()
    if resolved != root and root not in resolved.parents:
        raise ValidationError("the audio file is outside the voice workspace")
    if not resolved.is_file():
        raise ValidationError("the audio file does not exist")
    return resolved


__all__ = ["DesktopVoiceService", "VoicePrincipal"]
