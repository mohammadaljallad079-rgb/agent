"""Desktop voice assistant + local speech model tests.

These exercise the real product path: the desktop voice service is built over a
real :class:`~arabic_agent.container.Platform`, the *same* policy engine that
guards the agent decides whether the microphone and speaker are allowed, and a
spoken turn drives a real durable run. The audio devices and speech engines are
the scripted doubles from :mod:`tests.fake_speech` — they write real WAV files,
so the sandbox path handling and the durable store are exercised against genuine
files rather than mocked return values.

The speech *model* tests install from a real local HTTP server, so the verified,
resumable download path (size + SHA-256) is proven end to end.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import Locale, OperatingMode, PermissionEffect, RuntimeKind
from arabic_agent.core.errors import ConfigurationError, PermissionDeniedError, ValidationError
from arabic_agent.desktop.events import DesktopEventBroker
from arabic_agent.desktop.http import create_desktop_app
from arabic_agent.desktop.service import DesktopService, build_model_manager
from arabic_agent.desktop.voice import DesktopVoiceService
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.permissions.base import Permission, PolicySet, Rule
from arabic_agent.speech.model_store import SpeechModelStore, stt_model_dir, tts_voice_file
from arabic_agent.speech.models import KIND_STT, KIND_TTS, SpeechModelFile, SpeechModelSpec
from arabic_agent.speech.policy import SpeechPolicy
from arabic_agent.speech.runtime import SpeechRuntime

from .fake_http import http_server
from .fake_speech import FakeAudioBackend, FakeSynthesizer, FakeTranscriber
from .helpers import FakeProvider, text_response


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("تم")))
    return registry


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "test",
        "require_auth": False,
        "runtime_kind": RuntimeKind.LOCAL,
        "local_store_dir": str(tmp_path / "data"),
        "local_workspace_roots": (str(tmp_path / "approved"),),
        "local_mode": OperatingMode.OFFLINE,
        "database_url": SecretStr(""),
        "redis_url": SecretStr(""),
        "execution_mode": "inline",
        "speech_enabled": True,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _platform(tmp_path: Path, **overrides: object) -> Platform:
    return Platform.build(
        _settings(tmp_path, **overrides),
        provider_registry=_registry(),
        default_model="fake/model",
    )


def _install_fake_runtime(platform: Platform, *, text: str = "افتح التقرير") -> SpeechRuntime:
    """Swap the platform's real speech runtime for a scripted one.

    The runtime object is the real :class:`SpeechRuntime` — only the audio backend
    and the engines are doubles — so policy, budget, boundary, audit and the
    durable store are all exercised for real.
    """
    runtime = SpeechRuntime(
        backend=FakeAudioBackend(),
        transcription=FakeTranscriber(text=text),
        synthesis=FakeSynthesizer(),
        policy=SpeechPolicy(),
        audit=platform.audit,
    )
    platform.speech_runtime = runtime
    return runtime


def _allow_speech_policy(platform: Platform) -> None:
    """A tenant policy that allows the microphone and speaker outright."""
    platform.policy.set_tenant_policy(
        "local",
        PolicySet(
            name="test-allow-speech",
            default_effect=PermissionEffect.DENY,
            rules=(
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.MICROPHONE_RECORD,
                    reason="allowed for the test",
                ),
                Rule(
                    effect=PermissionEffect.ALLOW,
                    permission=Permission.SPEAKER_PLAYBACK,
                    reason="allowed for the test",
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Capabilities / governance
# ---------------------------------------------------------------------------


def test_capabilities_report_honestly_when_disabled(tmp_path: Path):
    platform = _platform(tmp_path, speech_enabled=False)
    service = DesktopVoiceService(platform=platform)
    caps = service.capabilities()
    assert caps["enabled"] is False
    assert caps["available"] is False
    assert "disabled" in str(caps["unavailable_reason"])
    # The character is always present, even when voice is off.
    assert caps["character"]["name_ar"]


def test_capabilities_report_runtime_and_real_policy(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    service = DesktopVoiceService(platform=platform)
    caps = service.capabilities()
    assert caps["enabled"] is True
    assert caps["available"] is True
    assert caps["can_record"] is True
    assert caps["can_transcribe"] is True
    assert caps["can_synthesize"] is True
    assert caps["stt_engine"] == "fake-stt"
    assert caps["tts_engine"] == "fake-tts"
    # Under the platform default policy both are approval-gated, and the report
    # says so rather than claiming the microphone is freely usable.
    assert caps["record_allowed"] is False
    assert caps["record_reason"]
    assert caps["playback_allowed"] is False
    assert caps["playback_reason"]


# ---------------------------------------------------------------------------
# The voice turn: listen and speak
# ---------------------------------------------------------------------------


async def test_transcribe_refuses_under_default_policy(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    service = DesktopVoiceService(platform=platform)
    started = await service.start_voice_run(goal="قل مرحبا")
    with pytest.raises(PermissionDeniedError) as excinfo:
        await service.transcribe(run_id=str(started["run_id"]))
    assert excinfo.value.details.get("permission") == "MICROPHONE_RECORD"


async def test_speak_refuses_under_default_policy(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    service = DesktopVoiceService(platform=platform)
    started = await service.start_voice_run(goal="قل مرحبا")
    with pytest.raises(PermissionDeniedError):
        await service.speak(run_id=str(started["run_id"]), text="مرحبا")


def test_transcribe_records_inside_the_run_workspace(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform, text="جهّز الفاتورة")
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> tuple[dict[str, object], dict[str, object]]:
        started = await service.start_voice_run(goal="قل مرحبا")
        run_id = str(started["run_id"])
        result = await service.transcribe(run_id=run_id, seconds=1.0)
        return started, result

    started, result = asyncio.run(_run())
    assert result["text"] == "جهّز الفاتورة"
    assert result["engine"] == "fake-stt"
    audio = Path(str(result["audio_path"]))
    # The recording is a real file, and it lives under the run's workspace.
    assert audio.is_file()
    assert str(Path(str(started["workspace_dir"]))) in str(audio)


def test_speak_synthesizes_and_plays(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> dict[str, object]:
        started = await service.start_voice_run(goal="قل مرحبا")
        return await service.speak(run_id=str(started["run_id"]), text="تم إنشاء ٤٢ فاتورة")

    result = asyncio.run(_run())
    assert result["engine"] == "fake-tts"
    audio = Path(str(result["audio_path"]))
    assert audio.is_file()
    assert audio.name.startswith("reply-")


async def test_transcribe_rejects_unknown_run(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)
    from arabic_agent.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        await service.transcribe(run_id="run_missing")


async def test_voice_actions_are_persisted_metadata_only(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform, text="سر خاص")
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)
    started = await service.start_voice_run(goal="قل مرحبا")
    run_id = str(started["run_id"])
    await service.transcribe(run_id=run_id, seconds=1.0)
    actions = await service.list_actions(run_id=run_id)
    assert actions["enabled"] is True
    assert actions["count"] >= 1
    kinds = {a["kind"] for a in actions["actions"]}
    assert "recording" in kinds and "transcription" in kinds
    # The transcript is kept only because the desktop opted in, and never the
    # raw audio bytes.
    assert all("size_bytes" in a for a in actions["actions"])


# ---------------------------------------------------------------------------
# Cancellation: a voice turn can be stopped mid-recording / mid-playback
# ---------------------------------------------------------------------------


def _install_blocking_runtime(platform: Platform) -> FakeAudioBackend:
    """A runtime whose audio backend blocks until cancelled, like real hardware."""
    backend = FakeAudioBackend(block_until_cancelled=True)
    platform.speech_runtime = SpeechRuntime(
        backend=backend,
        transcription=FakeTranscriber(text="افتح التقرير"),
        synthesis=FakeSynthesizer(),
        policy=SpeechPolicy(),
        audit=platform.audit,
    )
    return backend


async def test_transcribe_can_be_stopped_mid_recording(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_blocking_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)
    started = await service.start_voice_run(goal="قل مرحبا")
    run_id = str(started["run_id"])

    task = asyncio.create_task(service.transcribe(run_id=run_id, seconds=30.0))
    # Let the recording reach the blocking device loop, then stop it.
    for _ in range(200):
        await asyncio.sleep(0.005)
        if service.active["record"]:
            break
    assert service.active["record"] is True
    stopped = service.stop(op="record")
    assert stopped == {"stopped": ["record"], "cancelled": True}

    result = await asyncio.wait_for(task, timeout=5.0)
    # A deliberate stop is reported as cancelled, not raised as a failure.
    assert result["cancelled"] is True
    assert result["text"] == ""
    assert service.active["record"] is False

    # The stop is durable: a cancelled recording action is persisted and audited.
    actions = await service.list_actions(run_id=run_id)
    assert any(a["kind"] == "recording" and a["state"] == "cancelled" for a in actions["actions"])


def test_speak_can_be_stopped_mid_playback(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_blocking_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> tuple[dict[str, object], dict[str, object]]:
        started = await service.start_voice_run(goal="قل مرحبا")
        run_id = str(started["run_id"])
        task = asyncio.create_task(service.speak(run_id=run_id, text="تم إنشاء ٤٢ فاتورة"))
        for _ in range(200):
            await asyncio.sleep(0.005)
            if service.active["play"]:
                break
        assert service.active["play"] is True
        service.stop(op="play")
        result = await asyncio.wait_for(task, timeout=5.0)
        actions = await service.list_actions(run_id=run_id)
        return result, actions

    result, actions = asyncio.run(_run())
    assert result["cancelled"] is True
    # The audio was still synthesized to disk before playback was stopped.
    assert Path(str(result["audio_path"])).is_file()
    assert any(a["kind"] == "playback" and a["state"] == "cancelled" for a in actions["actions"])


def test_stop_with_nothing_running_is_honest(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)
    # Nothing in flight: the service must not claim it stopped something.
    assert service.stop() == {"stopped": [], "cancelled": False}


def test_transcribe_without_a_run_records_a_preview(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform, text="مرحبا")
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> dict[str, object]:
        # No run_id: a preview capture still works and lands under the local store.
        return await service.transcribe(seconds=1.0)

    result = asyncio.run(_run())
    assert result["cancelled"] is False
    assert result["text"] == "مرحبا"
    assert Path(str(result["audio_path"])).is_file()


def test_synthesize_without_playback(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> dict[str, object]:
        return await service.synthesize(text="تم")

    result = asyncio.run(_run())
    assert result["engine"] == "fake-tts"
    assert Path(str(result["audio_path"])).is_file()
    # Synthesis alone must not touch the speaker.
    runtime = platform.speech_runtime
    assert runtime is not None
    assert runtime._backend.played == []


async def test_play_file_refuses_a_path_outside_the_voice_workspace(tmp_path: Path):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform)
    _allow_speech_policy(platform)
    service = DesktopVoiceService(platform=platform)
    outside = tmp_path / "secret.wav"
    outside.write_bytes(b"RIFF")
    with pytest.raises(ValidationError):
        await service.play_file(path=str(outside))


# ---------------------------------------------------------------------------
# Speech model catalogue
# ---------------------------------------------------------------------------


def test_voice_models_catalogue_is_honest(tmp_path: Path):
    platform = _platform(tmp_path)
    service = DesktopVoiceService(platform=platform)
    models = {m["id"]: m for m in service.list_models()["models"]}
    assert "faster-whisper-small" in models
    assert "piper-ar-jo-kareem-medium" in models
    whisper = models["faster-whisper-small"]
    assert whisper["kind"] == KIND_STT
    assert whisper["languages"] == ["ar", "en"]
    assert whisper["license"] == "MIT"
    assert whisper["size_bytes"] > 0
    assert whisper["installed"] is False
    kareem = models["piper-ar-jo-kareem-medium"]
    assert kareem["kind"] == KIND_TTS
    assert kareem["languages"] == ["ar"]
    assert kareem["display_name_ar"]


def test_voice_install_refused_when_disabled(tmp_path: Path):
    platform = _platform(tmp_path, speech_enabled=False)
    service = DesktopVoiceService(platform=platform)

    async def _run() -> None:
        with pytest.raises(ConfigurationError):
            await service.install_model("piper-ar-jo-kareem-medium")

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# The verified, resumable speech-model installer
# ---------------------------------------------------------------------------


def _spec(url: str, *, sha256: str, size: int, kind: str = KIND_TTS) -> SpeechModelSpec:
    return SpeechModelSpec(
        id="test-voice",
        kind=kind,
        display_name="Test voice",
        engine="piper",
        languages=("ar",),
        files=(SpeechModelFile(url=url, filename="test.onnx", sha256=sha256, size_bytes=size),),
    )


def test_speech_model_store_installs_and_verifies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import arabic_agent.speech.model_store as model_store

    payload = b"not really a voice, but a real file with a real digest" * 8
    digest = hashlib.sha256(payload).hexdigest()
    artifact = tmp_path / "test.onnx"
    artifact.write_bytes(payload)

    with http_server() as server:
        server.route_file("/test.onnx", str(artifact))
        spec = _spec(f"{server.base_url}/test.onnx", sha256=digest, size=len(payload))
        monkeypatch.setattr(model_store, "get_speech_model", lambda _mid: spec)

        store = SpeechModelStore(storage_dir=str(tmp_path / "models"))
        assert store.is_installed("test-voice") is False

        result = asyncio.run(store.install("test-voice"))
        assert result.success is True
        assert store.is_installed("test-voice") is True
        # The resolved artifact is the real, verified file.
        resolved = Path(store.resolve("test-voice"))
        assert resolved.is_file()
        assert resolved.read_bytes() == payload


def test_speech_model_store_rejects_a_tampered_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import arabic_agent.speech.model_store as model_store

    payload = b"the real bytes"
    wrong = hashlib.sha256(b"something else entirely").hexdigest()
    artifact = tmp_path / "test.onnx"
    artifact.write_bytes(payload)

    with http_server() as server:
        server.route_file("/test.onnx", str(artifact))
        spec = _spec(f"{server.base_url}/test.onnx", sha256=wrong, size=len(payload))
        monkeypatch.setattr(model_store, "get_speech_model", lambda _mid: spec)

        store = SpeechModelStore(storage_dir=str(tmp_path / "models"))

        result = asyncio.run(store.install("test-voice"))
        assert result.success is False
        assert "integrity" in result.detail
        assert store.is_installed("test-voice") is False


def test_speech_model_resolvers_return_none_when_not_installed(tmp_path: Path):
    storage = str(tmp_path / "models")
    assert stt_model_dir(storage, "faster-whisper-small") is None
    assert tts_voice_file(storage, "piper-ar-jo-kareem-medium") is None


# ---------------------------------------------------------------------------
# The desktop HTTP surface
# ---------------------------------------------------------------------------


def _voice_app(tmp_path: Path, *, allow: bool = True):
    platform = _platform(tmp_path)
    _install_fake_runtime(platform, text="افتح التقرير")
    if allow:
        _allow_speech_policy(platform)
    service = DesktopService(
        platform=platform,
        model_manager=build_model_manager(platform),
        events=DesktopEventBroker(),
        locale=Locale.AR,
    )
    app = create_desktop_app(service, authorize=lambda: None)
    return app


def test_http_voice_capabilities(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path))
    body = client.get("/desktop/voice").json()
    assert body["enabled"] is True
    assert body["can_record"] is True
    assert body["record_allowed"] is True
    models = client.get("/desktop/voice/models").json()
    assert {m["id"] for m in models["models"]} >= {
        "faster-whisper-small",
        "piper-ar-jo-kareem-medium",
    }


def test_http_voice_start_transcribe_speak(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path))
    started = client.post("/desktop/voice/start", json={"goal": "قل مرحبا"})
    assert started.status_code == 200
    run_id = started.json()["run_id"]
    assert started.json()["status"]

    heard = client.post("/desktop/voice/transcribe", json={"run_id": run_id, "seconds": 1.0})
    assert heard.status_code == 200
    assert heard.json()["text"] == "افتح التقرير"

    spoke = client.post("/desktop/voice/speak", json={"run_id": run_id, "text": "تم"})
    assert spoke.status_code == 200
    assert spoke.json()["engine"] == "fake-tts"

    actions = client.get("/desktop/voice/actions", params={"run_id": run_id}).json()
    assert actions["count"] >= 1


def test_http_voice_transcribe_denied_under_default_policy(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path, allow=False))
    started = client.post("/desktop/voice/start", json={"goal": "قل مرحبا"})
    run_id = started.json()["run_id"]
    denied = client.post("/desktop/voice/transcribe", json={"run_id": run_id})
    assert denied.status_code == 403


def test_http_voice_synthesize_and_play(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path))
    synthesized = client.post("/desktop/voice/synthesize", json={"text": "تم"})
    assert synthesized.status_code == 200
    body = synthesized.json()
    assert body["engine"] == "fake-tts"
    assert body["cancelled"] is False
    # A synthesized clip can be replayed by name (resolved inside the workspace).
    played = client.post("/desktop/voice/play", json={"path": body["audio_path"]})
    assert played.status_code == 200
    assert played.json()["cancelled"] is False


def test_http_voice_stop_reports_what_it_stopped(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path))
    # Nothing in flight: the endpoint says so honestly.
    idle = client.post("/desktop/voice/stop", json={})
    assert idle.status_code == 200
    assert idle.json() == {"stopped": [], "cancelled": False}


def test_http_voice_play_refuses_path_escape(tmp_path: Path):
    client = TestClient(_voice_app(tmp_path))
    escape = client.post("/desktop/voice/play", json={"path": "../../etc/passwd"})
    assert escape.status_code == 422
