"""Shared pytest fixtures."""

from __future__ import annotations

import pytest

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, Responder


@pytest.fixture()
def settings(tmp_path) -> Settings:
    """Test settings: small budgets, temp workspace, no real credentials."""
    return Settings(
        environment="test",
        require_auth=False,
        local_store_dir=str(tmp_path / "data"),
        max_context_tokens=4000,
        max_steps_per_run=20,
        max_tool_retries=2,
        max_replans=2,
        max_run_seconds=30.0,
    )


@pytest.fixture()
def make_platform(settings: Settings):
    """Return a factory building a platform around a responder."""

    def factory(respond: Responder, *, extra_tools: list | None = None) -> Platform:
        registry = ProviderRegistry()
        registry.register(FakeProvider(respond))
        return Platform.build(
            settings,
            provider_registry=registry,
            default_model="fake/model",
            extra_tools=extra_tools,
        )

    return factory
