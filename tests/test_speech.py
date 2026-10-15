"""Speech / voice: policy, runtime, tools, durable store and container wiring.

These are *real* tests, not mocks of the code under test:

* the runtime is exercised against scripted backends/engines that write real WAV
  files, so size checks, sandbox path handling and the store see genuine data;
* the tools run through the real executor, policy engine, approval store and
  audit log, proving voice is not a bypass of the permission layer;
* the durable local store is tested against a real SQLite file created by the
  real migration chain, proving an action survives a restart;
* the container wiring is checked by building a real local platform.

Nothing asserts audio was captured or spoken without a backend having actually
accepted the call.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.agent.executor import ExecutionOutcomeKind, ToolExecutor
from arabic_agent.audit.base import AuditEventType, AuditLog
from arabic_agent.audit.memory_sink import InMemoryAuditSink
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import (
    DataBoundary,
    DataSensitivity,
    OperatingMode,
    PermissionEffect,
    RuntimeKind,
)
from arabic_agent.core.errors import (
    ConfigurationError,
    PermissionDeniedError,
    ResourceExhaustedError,
    ValidationError,
)
from arabic_agent.local.boundary import DataBoundaryPolicy
from arabic_agent.permissions.approvals import InMemoryApprovalStore
from arabic_agent.permissions.base import Permission, PolicySet, Principal, Rule
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.speech.cancel import CancelledError
from arabic_agent.speech.policy import SpeechPolicy
from arabic_agent.speech.runtime import SpeechRuntime
from arabic_agent.speech.store import (
    InMemorySpeechStore,
    SpeechActionKind,
    SpeechRecord,
    SpeechStore,
)
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.speech import (
    SPEECH_TOOLS,
    RecordAudioTool,
    SpeechActionsTool,
    SpeechCapabilitiesTool,
    SpeechToTextTool,
    TextToSpeechTool,
)
from arabic_agent.tools.registry import ToolRegistry

from .fake_speech import (
    FakeAudioBackend,
    FakeSynthesizer,
    FakeTranscriber,
    FakeUnavailableAudioBackend,
    NullSynthesizer,
    NullTranscriber,
)

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


def test_policy_rejects_nonpositive_recording():
    policy = SpeechPolicy()
    with pytest.raises(ValidationError):
        policy.check_recording_seconds(0)
    with pytest.raises(ValidationError):
        policy.check_recording_seconds(-1)


def test_policy_rejects_overlong_recording():
    policy = SpeechPolicy(max_recording_seconds=10)
    with pytest.raises(ResourceExhaustedError):
        policy.check_recording_seconds(11)


def test_policy_rejects_empty_and_oversized_synthesis_text():
    policy = SpeechPolicy(max_synthesis_characters=5)
    with pytest.raises(ValidationError):
        policy.check_synthesis_text("   ")
    with pytest.raises(ResourceExhaustedError):
        policy.check_synthesis_text("123456")


def test_policy_rejects_oversized_audio():
    policy = SpeechPolicy(max_audio_bytes=10)
    with pytest.raises(ResourceExhaustedError):
        policy.check_audio_size(11)
    policy.check_audio_size(10)


# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


def _exists(path: str) -> bool:
    return os.path.exists(path)


def _runtime(
    *,
    backend=None,
    transcription=None,
    synthesis=None,
    policy: SpeechPolicy | None = None,
    boundary: DataBoundaryPolicy | None = None,
    store: SpeechStore | None = None,
    allow_cloud: bool = False,
    sink: InMemoryAuditSink | None = None,
) -> tuple[SpeechRuntime, InMemoryAuditSink]:
    sink = sink or InMemoryAuditSink()
    runtime = SpeechRuntime(
        backend=backend or FakeAudioBackend(),
        transcription=transcription or FakeTranscriber(),
        synthesis=synthesis or FakeSynthesizer(),
        policy=policy or SpeechPolicy(),
        audit=AuditLog([sink]),
        boundary=boundary or DataBoundaryPolicy(OperatingMode.ONLINE),
        store=store,
        allow_cloud=allow_cloud,
    )
    return runtime, sink


async def test_runtime_records_real_wav_and_audits_metadata_only(tmp_path):
    runtime, sink = _runtime()
    clip = await runtime.record(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        output_path=str(tmp_path / "a.wav"),
        seconds=1.0,
    )
    assert _exists(clip.path)
    assert clip.size_bytes > 0
    assert clip.duration_seconds == 1.0
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    recorded = [e for e in events if e.type is AuditEventType.SPEECH_RECORDED]
    assert len(recorded) == 1
    # Metadata only — never the audio bytes.
    assert recorded[0].data["size_bytes"] == clip.size_bytes
    assert "payload" not in str(recorded[0].data)


async def test_runtime_refuses_when_backend_unavailable(tmp_path):
    runtime, _ = _runtime(backend=FakeUnavailableAudioBackend())
    with pytest.raises(ConfigurationError):
        await runtime.record(
            tenant_id="t",
            user_id="u",
            run_id="r",
            output_path=str(tmp_path / "a.wav"),
        )


async def test_runtime_enforces_recording_budget(tmp_path):
    runtime, _ = _runtime(policy=SpeechPolicy(max_recordings_per_run=1))
    await runtime.record(
        tenant_id="t", user_id="u", run_id="r", output_path=str(tmp_path / "a.wav")
    )
    with pytest.raises(ResourceExhaustedError):
        await runtime.record(
            tenant_id="t", user_id="u", run_id="r", output_path=str(tmp_path / "b.wav")
        )


async def test_runtime_enforces_synthesis_budget(tmp_path):
    runtime, _ = _runtime(policy=SpeechPolicy(max_syntheses_per_run=1))
    await runtime.synthesize(
        tenant_id="t",
        user_id="u",
        run_id="r",
        text="مرحبا",
        output_path=str(tmp_path / "a.wav"),
    )
    with pytest.raises(ResourceExhaustedError):
        await runtime.synthesize(
            tenant_id="t",
            user_id="u",
            run_id="r",
            text="مرحبا",
            output_path=str(tmp_path / "b.wav"),
        )


async def test_runtime_transcribe_keeps_text_only_when_opted_in(tmp_path):
    store = InMemorySpeechStore()
    runtime, _ = _runtime(store=store)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF fake")
    result = await runtime.transcribe(
        tenant_id="t", user_id="u", run_id="r", audio_path=str(audio), keep_text=False
    )
    assert result.text == "مرحبا"
    records = await store.list_for_run(tenant_id="t", run_id="r")
    assert records[-1].kind is SpeechActionKind.TRANSCRIPTION
    # Not kept: the transcript must not be persisted.
    assert records[-1].text == ""

    await runtime.transcribe(
        tenant_id="t", user_id="u", run_id="r", audio_path=str(audio), keep_text=True
    )
    records = await store.list_for_run(tenant_id="t", run_id="r")
    assert records[-1].text == "مرحبا"


async def test_runtime_refuses_cloud_engine_when_disabled(tmp_path):
    runtime, _ = _runtime(transcription=FakeTranscriber(is_cloud=True), allow_cloud=False)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(PermissionDeniedError):
        await runtime.transcribe(tenant_id="t", user_id="u", run_id="r", audio_path=str(audio))


async def test_runtime_refuses_cloud_engine_offline_even_when_enabled(tmp_path):
    boundary = DataBoundaryPolicy(OperatingMode.OFFLINE)
    runtime, sink = _runtime(
        transcription=FakeTranscriber(is_cloud=True),
        boundary=boundary,
        allow_cloud=True,
    )
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    with pytest.raises(PermissionDeniedError):
        await runtime.transcribe(tenant_id="t", user_id="u", run_id="r", audio_path=str(audio))
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    assert any(e.type is AuditEventType.SPEECH_BOUNDARY_DECISION for e in events)
    # A LOCAL boundary decision is always allowed, proving the deny above was
    # the remote-egress rule and not a blanket failure.
    decision = boundary.evaluate(
        boundary=DataBoundary.LOCAL, sensitivity=DataSensitivity.CONFIDENTIAL
    )
    assert decision.allowed


async def test_runtime_speak_synthesizes_then_plays(tmp_path):
    backend = FakeAudioBackend()
    runtime, sink = _runtime(backend=backend)
    result = await runtime.speak(
        tenant_id="t",
        user_id="u",
        run_id="r",
        text="مرحبا بك",
        output_path=str(tmp_path / "out.wav"),
    )
    assert _exists(result.clip.path)
    assert backend.played == [result.clip.path]
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    assert any(e.type is AuditEventType.SPEECH_SYNTHESIZED for e in events)
    assert any(e.type is AuditEventType.SPEECH_PLAYED for e in events)


async def test_runtime_cancelled_recording_is_persisted_and_audited(tmp_path):
    store = InMemorySpeechStore()
    runtime, sink = _runtime(store=store)
    # ``should_cancel`` is already true, so the backend raises before writing.
    clip_path = tmp_path / "a.wav"

    def _cancel() -> bool:
        return True

    with pytest.raises(CancelledError):
        await runtime.record(
            tenant_id="t",
            user_id="u",
            run_id="r",
            output_path=str(clip_path),
            should_cancel=_cancel,
        )
    records = await store.list_for_run(tenant_id="t", run_id="r")
    assert records[-1].kind is SpeechActionKind.RECORDING
    assert records[-1].state.value == "cancelled"
    events = await sink.list_for_run(tenant_id="t", run_id="r")
    cancelled = [e for e in events if e.type is AuditEventType.SPEECH_CANCELLED]
    assert len(cancelled) == 1
    # A stopped recording is not also reported as successfully recorded.
    assert not any(e.type is AuditEventType.SPEECH_RECORDED for e in events)


async def test_runtime_listen_records_then_transcribes(tmp_path):
    runtime, _ = _runtime()
    clip, result = await runtime.listen(
        tenant_id="t",
        user_id="u",
        run_id="r",
        output_path=str(tmp_path / "cmd.wav"),
        seconds=1.0,
    )
    assert _exists(clip.path)
    assert result.text == "مرحبا"


async def test_runtime_capabilities_report_honestly_when_unavailable():
    runtime, _ = _runtime(
        backend=FakeUnavailableAudioBackend(),
        transcription=NullTranscriber(),
        synthesis=NullSynthesizer(),
    )
    caps = runtime.capabilities()
    assert caps.can_record is False
    assert caps.can_transcribe is False
    assert caps.can_synthesize is False
    assert caps.backend_unavailable_reason
    assert caps.unavailable_reasons


async def test_runtime_capabilities_report_available():
    runtime, _ = _runtime()
    caps = runtime.capabilities()
    assert caps.can_record and caps.can_play
    assert caps.can_transcribe and caps.can_synthesize
    assert "ar" in caps.languages


async def test_runtime_play_refuses_missing_file(tmp_path):
    runtime, _ = _runtime()
    with pytest.raises(ValidationError):
        await runtime.play(
            tenant_id="t",
            user_id="u",
            run_id="r",
            clip_path=str(tmp_path / "missing.wav"),
        )


# ---------------------------------------------------------------------------
# Durable store (real SQLite + real migrations)
# ---------------------------------------------------------------------------


async def test_local_speech_store_survives_restart(tmp_path):
    from arabic_agent.local.persistence.database import LocalDatabase
    from arabic_agent.local.persistence.stores import LocalSpeechActionStore

    db = LocalDatabase(str(tmp_path / "local.db"))
    await db.upgrade_async()
    store = LocalSpeechActionStore(db)
    await store.add(
        SpeechRecord(
            id="spc_1",
            tenant_id="tnt_1",
            user_id="usr_1",
            run_id="run_1",
            kind=SpeechActionKind.RECORDING,
            engine="fake",
            language="ar",
            duration_seconds=1.5,
            size_bytes=32000,
        )
    )
    await db.dispose()

    # A fresh database handle (as after an application restart) reads it back.
    db2 = LocalDatabase(str(tmp_path / "local.db"))
    store2 = LocalSpeechActionStore(db2)
    record = await store2.get("spc_1", tenant_id="tnt_1")
    assert record.duration_seconds == 1.5
    assert record.kind is SpeechActionKind.RECORDING
    await db2.dispose()


async def test_local_speech_store_isolates_tenants(tmp_path):
    from arabic_agent.core.errors import NotFoundError
    from arabic_agent.local.persistence.database import LocalDatabase
    from arabic_agent.local.persistence.stores import LocalSpeechActionStore

    db = LocalDatabase(str(tmp_path / "local.db"))
    await db.upgrade_async()
    store = LocalSpeechActionStore(db)
    await store.add(
        SpeechRecord(id="spc_x", tenant_id="tnt_a", user_id="u", kind=SpeechActionKind.PLAYBACK)
    )
    with pytest.raises(NotFoundError):
        await store.get("spc_x", tenant_id="tnt_b")
    await db.dispose()


# ---------------------------------------------------------------------------
# Tools through the real executor
# ---------------------------------------------------------------------------


def _allow_policy() -> PolicyEngine:
    engine = PolicyEngine()
    engine.set_tenant_policy(
        "tnt_1",
        PolicySet(
            name="allow-speech",
            rules=(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.MICROPHONE_RECORD,
                    reason="test allow",
                ),
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.SPEAKER_PLAYBACK,
                    reason="test allow",
                ),
            ),
        ),
    )
    return engine


def _executor(*, policy=None, backend=None, runtime=None):
    sink = InMemoryAuditSink()
    audit = AuditLog([sink])
    backend = backend or FakeAudioBackend()
    runtime = runtime or SpeechRuntime(
        backend=backend,
        transcription=FakeTranscriber(),
        synthesis=FakeSynthesizer(),
        policy=SpeechPolicy(),
        audit=audit,
        boundary=DataBoundaryPolicy(OperatingMode.ONLINE),
    )
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in SPEECH_TOOLS]),
        policy=policy or _allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=audit,
        tool_metadata={"speech_runtime": runtime},
    )
    return executor, sink, backend


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        tenant_id="tnt_1", user_id="usr_1", run_id="run_1", workspace_dir=str(tmp_path)
    )


def _principal() -> Principal:
    return Principal(tenant_id="tnt_1", user_id="usr_1")


def test_speech_tools_declare_permissions():
    assert RecordAudioTool.permissions == (Permission.MICROPHONE_RECORD,)
    assert SpeechToTextTool.permissions == (Permission.MICROPHONE_RECORD,)
    assert TextToSpeechTool.permissions == (Permission.SPEAKER_PLAYBACK,)
    assert SpeechCapabilitiesTool.permissions == ()
    assert SpeechActionsTool.permissions == ()


async def test_record_audio_requires_approval_under_default_policy(tmp_path):
    executor, _, _ = _executor(policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="record_audio",
        arguments={"seconds": 1},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.MICROPHONE_RECORD


async def test_text_to_speech_requires_approval_under_default_policy(tmp_path):
    executor, _, _ = _executor(policy=PolicyEngine())
    outcome = await executor.execute(
        tool_name="text_to_speech",
        arguments={"text": "مرحبا"},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.APPROVAL_REQUIRED
    assert outcome.approval is not None
    assert outcome.approval.permission is Permission.SPEAKER_PLAYBACK


async def test_record_audio_executes_when_allowed(tmp_path):
    executor, sink, backend = _executor()
    outcome = await executor.execute(
        tool_name="record_audio",
        arguments={"seconds": 1},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED, outcome
    assert outcome.result is not None and outcome.result.ok
    assert len(backend.recorded) == 1
    events = await sink.list_for_run(tenant_id="tnt_1", run_id="run_1")
    assert any(e.type is AuditEventType.SPEECH_RECORDED for e in events)


async def test_text_to_speech_executes_when_allowed(tmp_path):
    executor, _, backend = _executor()
    outcome = await executor.execute(
        tool_name="text_to_speech",
        arguments={"text": "مرحبا بك"},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED, outcome
    assert len(backend.played) == 1


async def test_record_audio_writes_inside_sandbox(tmp_path):
    executor, _, _ = _executor()
    # The sandbox refuses an escaping path before any device call.
    with pytest.raises(PermissionDeniedError):
        await executor.execute(
            tool_name="record_audio",
            arguments={"seconds": 1, "path": "../escape.wav"},
            context=_context(tmp_path),
            principal=_principal(),
        )
    assert not (tmp_path.parent / "escape.wav").exists()


async def test_capabilities_tool_reports_matrix(tmp_path):
    executor, _, _ = _executor()
    outcome = await executor.execute(
        tool_name="speech_capabilities",
        arguments={"include_devices": True},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED, outcome
    assert outcome.result is not None
    data = outcome.result.data
    assert data["can_record"] is True
    assert data["can_synthesize"] is True
    assert len(data["devices"]) == 2


async def test_speech_actions_tool_lists_records(tmp_path):
    executor, _, _ = _executor()
    await executor.execute(
        tool_name="record_audio",
        arguments={"seconds": 1},
        context=_context(tmp_path),
        principal=_principal(),
    )
    outcome = await executor.execute(
        tool_name="speech_actions",
        arguments={},
        context=_context(tmp_path),
        principal=_principal(),
    )
    assert outcome.kind is ExecutionOutcomeKind.COMPLETED, outcome
    assert outcome.result is not None
    assert outcome.result.data["count"] >= 1


async def test_speech_tools_fail_honestly_without_runtime(tmp_path):
    executor = ToolExecutor(
        registry=ToolRegistry([cls() for cls in SPEECH_TOOLS]),
        policy=_allow_policy(),
        approvals=InMemoryApprovalStore(),
        audit=AuditLog([InMemoryAuditSink()]),
    )
    with pytest.raises(ConfigurationError):
        await executor.execute(
            tool_name="speech_capabilities",
            arguments={},
            context=_context(tmp_path),
            principal=_principal(),
        )


# ---------------------------------------------------------------------------
# Container wiring
# ---------------------------------------------------------------------------


def _registry():
    from arabic_agent.models.registry import ProviderRegistry

    from .helpers import FakeProvider, text_response

    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    return registry


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "environment": "test",
        "require_auth": False,
        "local_store_dir": str(tmp_path / "data"),
        "runtime_kind": RuntimeKind.LOCAL,
        "database_url": SecretStr(""),
        "redis_url": SecretStr(""),
        "execution_mode": "inline",
        "speech_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_speech_settings_defaults():
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.speech_enabled is False
    assert settings.speech_allow_cloud is False
    assert settings.speech_stt_engine == "auto"
    assert settings.speech_tts_engine == "auto"


def test_platform_without_speech_registers_no_speech_tools(tmp_path):
    settings = _settings(tmp_path, speech_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert "record_audio" not in names
    assert "text_to_speech" not in names
    assert platform.speech_runtime is None


def test_platform_with_speech_builds_runtime_and_tools(tmp_path):
    settings = _settings(tmp_path, speech_enabled=True)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert {"speech_capabilities", "record_audio", "speech_to_text", "text_to_speech"} <= names
    assert platform.speech_runtime is not None
    # The backend and engines are real and honest about availability on this
    # host — never a silent "available" that fails on first use.
    caps = platform.speech_runtime.capabilities()
    if not caps.can_record:
        assert caps.backend_unavailable_reason
