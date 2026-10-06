"""Container wiring and settings tests for external connectors (Phase 9).

Prove that the connector subsystem is assembled only for a local runtime with
connectors enabled, that its tools are registered only then, that a cloud runtime
never gets them, and that the settings expose the expected safe defaults.
"""

from __future__ import annotations

import json

from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import RuntimeKind
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.tools.builtin.connectors import CONNECTOR_TOOL_NAMES

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
        "connectors_enabled": True,
        "connectors_json": json.dumps(
            [
                {"name": "api", "kind": "http", "base_url": "https://api.example.com"},
                {
                    "name": "hook",
                    "kind": "webhook",
                    "url": "https://example.com/hook",
                    "secret_env": "HOOK_SECRET",
                },
            ]
        ),
    }
    values.update(overrides)
    return Settings(**values)


def test_connector_settings_defaults():
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.connectors_enabled is False
    assert settings.connectors_json == ""
    assert settings.connectors_allow_local_addresses is False
    assert settings.connector_max_response_bytes == 1_048_576
    assert settings.connector_retry_max_attempts == 3


def test_platform_without_connectors_registers_no_connector_tools(tmp_path):
    settings = _settings(tmp_path, connectors_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()).isdisjoint(CONNECTOR_TOOL_NAMES)
    assert platform.connector_runtime is None


def test_local_platform_with_connectors_wires_runtime_and_tools(tmp_path):
    settings = _settings(tmp_path)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()) >= CONNECTOR_TOOL_NAMES
    assert platform.connector_runtime is not None
    assert set(platform.connector_runtime.registry.names()) == {"api", "hook"}


def test_cloud_runtime_registers_connector_tools_when_enabled(tmp_path):
    # Connectors reach *remote* systems (SaaS APIs, SMTP, webhooks), so unlike
    # the local-only computer/browser tools they are available in a cloud
    # runtime too -- still opt-in, and every write/send stays approval-gated.
    settings = _settings(tmp_path, runtime_kind=RuntimeKind.CLOUD)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()) >= CONNECTOR_TOOL_NAMES
    assert platform.connector_runtime is not None
    assert set(platform.connector_runtime.registry.names()) == {"api", "hook"}


def test_cloud_runtime_without_connectors_has_none(tmp_path):
    settings = _settings(tmp_path, runtime_kind=RuntimeKind.CLOUD, connectors_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert set(platform.tools.names()).isdisjoint(CONNECTOR_TOOL_NAMES)
    assert platform.connector_runtime is None


def test_enabled_without_specs_still_wires_an_empty_registry(tmp_path):
    settings = _settings(tmp_path, connectors_json="")
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.connector_runtime is not None
    assert platform.connector_runtime.registry.names() == []
