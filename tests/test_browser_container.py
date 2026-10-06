"""Container wiring and settings tests for browsing (Phase 8).

Prove that the browser subsystem is assembled only for a local runtime with
browsing enabled, that its tools are registered only then, that a cloud runtime
never gets browser tools, and that the settings expose the expected defaults and
the URL/domain policy they produce.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from arabic_agent.browser.backends import build_browser_backend
from arabic_agent.browser.backends.null import NullBrowserBackend
from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import RuntimeKind
from arabic_agent.models.registry import ProviderRegistry
from arabic_agent.tools.builtin.browser import BROWSER_TOOL_NAMES

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
        "browser_enabled": True,
        "local_allow_confidential_remote": True,
    }
    values.update(overrides)
    return Settings(**values)


def test_browser_settings_defaults():
    settings = Settings(environment="test", database_url="", redis_url="", local_store_dir="/tmp/x")
    assert settings.browser_enabled is False
    assert settings.browser_backend == "auto"
    assert settings.browser_headless is True
    assert settings.browser_content_policy == "local_only"
    assert settings.browser_allow_local_addresses is False
    assert settings.browser_allow_file_urls is False
    assert settings.browser_require_fresh_observation is True
    assert settings.browser_require_approval_for_submit is True


def test_platform_without_browsing_registers_no_browser_tools(tmp_path):
    settings = _settings(tmp_path, browser_enabled=False)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names.isdisjoint(BROWSER_TOOL_NAMES)
    assert platform.browser_runtime is None


def test_local_platform_with_browsing_wires_runtime_and_tools(tmp_path):
    settings = _settings(tmp_path)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names >= BROWSER_TOOL_NAMES
    assert platform.browser_runtime is not None
    # Browsing alone supplies a vision attachment provider.
    assert platform.attachment_provider is not None


def test_cloud_runtime_never_registers_browser_tools(tmp_path):
    settings = _settings(tmp_path, runtime_kind=RuntimeKind.CLOUD, browser_enabled=True)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    names = set(platform.tools.names())
    assert names.isdisjoint(BROWSER_TOOL_NAMES)
    assert platform.browser_runtime is None


def test_unknown_backend_degrades_to_null():
    backend = build_browser_backend(backend="does-not-exist", download_dir="/tmp/x")
    assert isinstance(backend, NullBrowserBackend)
    assert not backend.available
    assert "unknown browser backend" in backend.unavailable_reason


def test_explicit_null_backend_reports_reason():
    backend = build_browser_backend(backend="null", download_dir="/tmp/x")
    assert isinstance(backend, NullBrowserBackend)
    assert not backend.available


def test_auto_backend_is_honest_about_availability():
    backend = build_browser_backend(backend="auto", download_dir="/tmp/x")
    # Either a real backend that declares itself available, or a null backend
    # that carries a reason -- never a backend that pretends to work.
    if isinstance(backend, NullBrowserBackend):
        assert not backend.available
        assert backend.unavailable_reason
    else:
        assert backend.available


def test_browser_url_policy_reflects_settings(tmp_path):
    settings = _settings(
        tmp_path,
        browser_allowed_domains=("example.com",),
        browser_blocked_domains=("evil.com",),
        browser_allow_local_addresses=True,
    )
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.browser_runtime is not None
    policy = platform.browser_runtime.policy
    assert policy.url_policy.allowed_domains == ("example.com",)
    assert policy.url_policy.blocked_domains == ("evil.com",)
    assert policy.url_policy.allow_local_addresses is True
    assert not policy.url_policy.evaluate("https://other.com/").allowed
    assert not policy.url_policy.evaluate("https://evil.com/").allowed


def test_invalid_content_policy_falls_back_to_local_only(tmp_path):
    settings = _settings(tmp_path, browser_content_policy="nonsense")
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.browser_runtime is not None
    assert platform.browser_runtime.boundary.policy.value == "local_only"


@pytest.mark.parametrize("policy", ["never_store", "local_only", "cloud_allowed"])
def test_valid_content_policies_are_honoured(tmp_path, policy):
    settings = _settings(tmp_path, browser_content_policy=policy)
    platform = Platform.build(settings, provider_registry=_registry(), default_model="fake/model")
    assert platform.browser_runtime is not None
    assert platform.browser_runtime.boundary.policy.value == policy
