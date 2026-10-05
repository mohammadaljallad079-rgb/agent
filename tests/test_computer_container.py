"""Container wiring and settings tests for computer control (Phase 7).

Prove that the computer subsystem is assembled only for a local runtime with
computer control enabled, that its tools are registered only then, and that the
settings expose the expected defaults and validation.
"""

from __future__ import annotations

from pydantic import SecretStr

from arabic_agent.computer.backends import build_computer_backend
from arabic_agent.computer.backends.null import NullComputerBackend
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import PlatformKind, RuntimeKind
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.tools.builtin.computer import COMPUTER_TOOL_NAMES

from .helpers import FakeProvider, text_response


def _registry() -> ProviderRegistry:
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    return registry


def _settings(tmp_path, **overrides) -> Settings:
    values = {
        "environment": "test",
        "require_auth": False,
        "local_store_dir": str(tmp_path / "data"),
        "runtime_kind": RuntimeKind.LOCAL,
        "database_url": SecretStr(""),
        "redis_url": SecretStr(""),
        "execution_mode": "inline",
        "computer_control_enabled": True,
        "local_allow_confidential_remote": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_computer_settings_defaults():
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.computer_control_enabled is False
    assert settings.computer_backend == "auto"
    assert settings.computer_screen_policy == "local_only"
    assert settings.computer_ocr_languages == ("ara", "eng")


def test_platform_without_computer_control_registers_no_computer_tools(tmp_path):
    settings = _settings(tmp_path, computer_control_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names.isdisjoint(COMPUTER_TOOL_NAMES)
    assert platform.computer_runtime is None


def test_local_platform_with_computer_control_wires_runtime_and_tools(tmp_path):
    settings = _settings(tmp_path)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names >= COMPUTER_TOOL_NAMES
    assert platform.computer_runtime is not None
    assert platform.attachment_provider is not None


def test_cloud_runtime_never_registers_computer_tools(tmp_path):
    settings = _settings(
        tmp_path,
        runtime_kind=RuntimeKind.CLOUD,
        computer_control_enabled=True,
    )
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names.isdisjoint(COMPUTER_TOOL_NAMES)


def test_build_backend_returns_null_for_unimplemented_platforms():
    for kind in (PlatformKind.WINDOWS, PlatformKind.MACOS):
        backend = build_computer_backend(platform=kind)
        assert isinstance(backend, NullComputerBackend)
        assert backend.available is False
        assert backend.unavailable_reason


def test_build_backend_for_linux_reports_availability_honestly():
    backend = build_computer_backend(platform=PlatformKind.LINUX)
    # Either a working X11 backend or an honest Null backend — never a silent
    # "available" that fails on first use.
    assert backend.available in (True, False)
    if not backend.available:
        assert backend.unavailable_reason


def test_invalid_screen_policy_falls_back_to_local_only(tmp_path):
    # An unknown policy must not crash construction; it fails closed.
    settings = _settings(tmp_path, computer_screen_policy="not-a-policy")
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.computer_runtime is not None
    assert platform.computer_runtime.boundary.policy.value == "local_only"


def test_computer_ocr_languages_accepts_csv_env(monkeypatch):
    monkeypatch.setenv("AAP_COMPUTER_OCR_LANGUAGES", "ara,eng,fra")
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.computer_ocr_languages == ("ara", "eng", "fra")


def test_capabilities_declare_computer_control_unimplemented_by_default():
    from arabic_agent.local.capabilities import LocalCapabilityRegistry

    registry = LocalCapabilityRegistry()
    implemented = {c.id for c in registry.implemented()}
    assert "screen.capture" not in implemented
    assert "input.pointer" not in implemented
    assert "input.keyboard" not in implemented
    assert "window.control" not in implemented
    # The vocabulary is still declared, so policy/UI can reason about it.
    assert registry.has("screen.capture")
    assert registry.has("input.pointer")


def test_capabilities_mark_computer_control_implemented_when_enabled():
    from arabic_agent.local.capabilities import LocalCapabilityRegistry

    registry = LocalCapabilityRegistry(computer_control=True)
    implemented = {c.id for c in registry.implemented()}
    assert {"screen.capture", "input.pointer", "input.keyboard", "window.control"} <= implemented
    # The coarse catch-all stays declared-but-not-implemented.
    assert "input.control" not in implemented


def test_enabling_computer_control_marks_capabilities_in_local_platform(tmp_path):
    settings = _settings(tmp_path)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.local_runtime is not None
    caps = {c.id: c for c in platform.local_runtime.capabilities.all()}
    assert caps["screen.capture"].implemented is True
    assert caps["screen.capture"].tool_name == "computer_screen"


def test_disabling_computer_control_keeps_capabilities_unimplemented(tmp_path):
    settings = _settings(tmp_path, computer_control_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.local_runtime is not None
    caps = {c.id: c for c in platform.local_runtime.capabilities.all()}
    assert caps["screen.capture"].implemented is False
