"""Speech tools: hear a spoken command, and answer by voice.

These are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so they run
through the existing executor: the permission/policy layer decides allow / deny /
requires-approval, approvals pause the run, and every call is audited. The tools
themselves contain no device or model logic — they translate model arguments into
a :class:`~arabic_agent.speech.runtime.SpeechRuntime` call, which enforces policy,
the per-run budget, the data boundary and audit.

The audio path is resolved through the *filesystem sandbox*, so a request can only
read or write a file the agent is already allowed to touch — it can never be used
to read an arbitrary host file or drop a recording anywhere on disk. They are
registered only for a local runtime and only when speech is enabled, so on a
cloud build the model cannot even name them.
"""

from __future__ import annotations

from typing import Any, ClassVar

from ...core.errors import ConfigurationError, ValidationError
from ...core.ids import PREFIX_AUDIO, new_id
from ...permissions.base import Permission
from ...speech.character import SUPER_AI_CHARACTER
from ...speech.runtime import SpeechRuntime
from ..base import Tool, ToolContext, ToolResult
from .local import sandbox_for


def speech_runtime(context: ToolContext) -> SpeechRuntime:
    """Fetch the speech runtime from the tool context, or fail honestly."""
    runtime = context.metadata.get("speech_runtime")
    if not isinstance(runtime, SpeechRuntime):
        raise ConfigurationError("speech is not available in this runtime")
    return runtime


class _SpeechTool(Tool):
    """Shared base: declares that it needs the speech runtime."""

    @staticmethod
    def _runtime(context: ToolContext) -> SpeechRuntime:
        return speech_runtime(context)


class SpeechCapabilitiesTool(_SpeechTool):
    name = "speech_capabilities"
    description = (
        "Report what speech can actually do in this runtime: whether the "
        "microphone and speaker are available, which speech-to-text and "
        "text-to-speech engines are installed, whether any of them is a cloud "
        "engine (audio would leave the machine), the supported languages, and "
        "the audio devices present. Use this before trying to listen or speak, "
        "so a missing engine is reported honestly instead of failing mid-run."
    )
    # Read-only capability reporting; no permission is required.
    permissions: tuple[Permission, ...] = ()
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "include_devices": {
                "type": "boolean",
                "description": "Also enumerate the machine's audio devices",
            }
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        caps = runtime.capabilities()
        devices = (
            await runtime.list_devices(
                tenant_id=context.tenant_id, user_id=context.user_id, run_id=context.run_id
            )
            if arguments.get("include_devices")
            else []
        )
        data: dict[str, Any] = {
            "can_record": caps.can_record,
            "can_play": caps.can_play,
            "can_transcribe": caps.can_transcribe,
            "can_synthesize": caps.can_synthesize,
            "backend": caps.backend,
            "backend_available": caps.backend_available,
            "backend_unavailable_reason": caps.backend_unavailable_reason,
            "stt_engines": list(caps.stt_engines),
            "tts_engines": list(caps.tts_engines),
            "cloud_stt": caps.cloud_stt,
            "cloud_tts": caps.cloud_tts,
            "languages": list(caps.languages),
            "unavailable_reasons": list(caps.unavailable_reasons),
            "character": SUPER_AI_CHARACTER.to_public_dict(),
            "devices": [
                {
                    "id": d.id,
                    "name": d.name,
                    "kind": d.kind,
                    "default": d.is_default,
                    "available": d.available,
                    "unavailable_reason": d.unavailable_reason,
                }
                for d in devices
            ],
        }
        summary = (
            "speech is available"
            if caps.can_record or caps.can_transcribe or caps.can_synthesize
            else "speech is not available in this runtime"
        )
        return ToolResult.success(summary, data=data)


class RecordAudioTool(_SpeechTool):
    name = "record_audio"
    description = (
        "Record audio from the microphone for a number of seconds and save it as "
        "a WAV file in the workspace. Recording captures ambient sound the user "
        "did not author, so it always requires approval. Use speech_to_text to "
        "turn the recording into text."
    )
    permissions: tuple[Permission, ...] = (Permission.MICROPHONE_RECORD,)
    side_effecting = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "seconds": {
                "type": "number",
                "description": "How many seconds to record (bounded by policy)",
            },
            "path": {
                "type": "string",
                "description": "Workspace-relative output path (a name is generated if omitted)",
            },
            "device_id": {
                "type": "string",
                "description": "Input device id (default device if omitted)",
            },
            "language": {
                "type": "string",
                "description": "Language hint for later transcription, e.g. ar",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        seconds = float(arguments.get("seconds") or 5.0)
        if seconds <= 0:
            raise ValidationError("seconds must be positive")
        requested = arguments.get("path") or f"{new_id(PREFIX_AUDIO)}.wav"
        if not isinstance(requested, str) or not requested.strip():
            raise ValidationError("path must be a non-empty string")
        resolved = sandbox_for(context).resolve_for_write(requested)
        clip = await runtime.record(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            output_path=str(resolved),
            device_id=arguments.get("device_id") or "",
            seconds=seconds,
            language=arguments.get("language") or "",
        )
        return ToolResult.success(
            f"recorded {clip.duration_seconds:.1f}s of audio",
            data={
                "path": clip.path,
                "duration_seconds": clip.duration_seconds,
                "size_bytes": clip.size_bytes,
                "sample_rate_hz": clip.sample_rate_hz,
                "backend": clip.engine,
            },
        )


class SpeechToTextTool(_SpeechTool):
    name = "speech_to_text"
    description = (
        "Transcribe an audio file in the workspace to text (Arabic by default). "
        "Give a path to a recorded file, or omit it to record briefly from the "
        "microphone and transcribe in one step. The result is the recognised "
        "text; the transcript is only stored when keep_text is set. If the "
        "active engine is a cloud engine, the audio leaves the machine — this is "
        "refused unless cloud speech is enabled and the data boundary allows it."
    )
    # Transcribing reads a sandbox audio file; recording (when no path is given)
    # is gated by the microphone permission below.
    permissions: tuple[Permission, ...] = (Permission.MICROPHONE_RECORD,)
    side_effecting = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Workspace-relative audio file to transcribe (records from the mic if omitted)"
                ),
            },
            "seconds": {
                "type": "number",
                "description": "Seconds to record when no path is given",
            },
            "device_id": {
                "type": "string",
                "description": "Input device id (default device if omitted)",
            },
            "language": {"type": "string", "description": "Language code, e.g. ar or en"},
            "keep_text": {
                "type": "boolean",
                "description": "Persist the recognised transcript in the action history",
            },
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        language = arguments.get("language") or ""
        keep_text = bool(arguments.get("keep_text"))
        path = arguments.get("path")
        if path:
            if not isinstance(path, str) or not path.strip():
                raise ValidationError("path must be a non-empty string")
            resolved = sandbox_for(context).resolve_for_read(path)
            result = await runtime.transcribe(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                audio_path=str(resolved),
                language=language,
                keep_text=keep_text,
            )
        else:
            seconds = float(arguments.get("seconds") or 5.0)
            if seconds <= 0:
                raise ValidationError("seconds must be positive")
            clip_path = sandbox_for(context).resolve_for_write(f"{new_id(PREFIX_AUDIO)}.wav")
            _clip, result = await runtime.listen(
                tenant_id=context.tenant_id,
                user_id=context.user_id,
                run_id=context.run_id,
                output_path=str(clip_path),
                seconds=seconds,
                device_id=arguments.get("device_id") or "",
                language=language,
                keep_text=keep_text,
            )
        data = {
            "text": result.text,
            "language": result.language,
            "engine": result.engine,
            "duration_seconds": result.duration_seconds,
            "segments": len(result.segments),
        }
        if not result.text:
            return ToolResult.success("no speech was recognised in the audio", data=data)
        return ToolResult.success(f"recognised {len(result.text)} characters of speech", data=data)


class TextToSpeechTool(_SpeechTool):
    name = "text_to_speech"
    description = (
        "Speak text aloud through the speaker (Arabic by default), synthesizing "
        "it and playing it in one step. Playing audio is an audible physical side "
        "effect, so it always requires approval. If the active engine is a cloud "
        "engine, the text leaves the machine — this is refused unless cloud "
        "speech is enabled and the data boundary allows it."
    )
    permissions: tuple[Permission, ...] = (Permission.SPEAKER_PLAYBACK,)
    side_effecting = True
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "The text to speak"},
            "voice": {
                "type": "string",
                "description": "Preferred voice (engine default if omitted)",
            },
            "language": {"type": "string", "description": "Language code, e.g. ar or en"},
            "device_id": {
                "type": "string",
                "description": "Output device id (default device if omitted)",
            },
        },
        "required": ["text"],
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        text = arguments["text"]
        if not isinstance(text, str) or not text.strip():
            raise ValidationError("text must be a non-empty string")
        clip_path = sandbox_for(context).resolve_for_write(f"{new_id(PREFIX_AUDIO)}.wav")
        result = await runtime.speak(
            tenant_id=context.tenant_id,
            user_id=context.user_id,
            run_id=context.run_id,
            text=text,
            output_path=str(clip_path),
            voice=arguments.get("voice") or "",
            language=arguments.get("language") or "",
            device_id=arguments.get("device_id") or "",
        )
        return ToolResult.success(
            f"spoke {len(text)} characters",
            data={
                "path": result.clip.path,
                "engine": result.engine,
                "voice": result.voice,
                "language": result.language,
                "duration_seconds": result.clip.duration_seconds,
            },
        )


class SpeechActionsTool(_SpeechTool):
    name = "speech_actions"
    description = (
        "List speech actions this machine has performed, most recent first, with "
        "their kind (recording, transcription, synthesis, playback), engine, "
        "duration, language and outcome. Use this to review or reconcile past "
        "voice activity. Transcripts appear only when they were kept explicitly."
    )
    # Read-only discovery of local action metadata; no permission is required
    # (records are tenant-isolated and contain no audio).
    permissions: tuple[Permission, ...] = ()
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "run_id": {"type": "string", "description": "Only actions from this run"},
            "limit": {"type": "integer", "description": "Maximum actions to return (1-200)"},
        },
        "additionalProperties": False,
    }

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        runtime = self._runtime(context)
        limit = int(arguments.get("limit") or 50)
        limit = max(1, min(limit, 200))
        records = await runtime.list_actions(
            tenant_id=context.tenant_id,
            run_id=arguments.get("run_id") or None,
            limit=limit,
        )
        data = {
            "count": len(records),
            "actions": [
                {
                    "id": r.id,
                    "kind": r.kind.value,
                    "state": r.state.value,
                    "engine": r.engine,
                    "language": r.language,
                    "duration_seconds": r.duration_seconds,
                    "size_bytes": r.size_bytes,
                    "text": r.text,
                    "run_id": r.run_id,
                    "created_at": r.created_at.isoformat(),
                }
                for r in records
            ],
        }
        if not records:
            return ToolResult.success("no speech actions have been performed", data=data)
        return ToolResult.success(f"{len(records)} speech action(s)", data=data)


SPEECH_TOOLS: tuple[type[Tool], ...] = (
    SpeechCapabilitiesTool,
    RecordAudioTool,
    SpeechToTextTool,
    TextToSpeechTool,
    SpeechActionsTool,
)


__all__ = [
    "SPEECH_TOOLS",
    "RecordAudioTool",
    "SpeechActionsTool",
    "SpeechCapabilitiesTool",
    "SpeechToTextTool",
    "TextToSpeechTool",
    "speech_runtime",
]
