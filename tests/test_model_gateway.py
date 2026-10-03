"""Model gateway tests: routing, retry, fallback, error classification."""

from __future__ import annotations

import pytest

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ModelCapability
from arabic_agent.core.errors import (
    ConfigurationError,
    ExternalServiceError,
    TransientError,
    ValidationError,
)
from arabic_agent.models.base import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
)
from arabic_agent.models.gateway import ModelGateway
from arabic_agent.models.registry import ProviderRegistry


class _Provider(ModelProvider):
    def __init__(self, name: str, *, fail_times: int = 0, error=None) -> None:
        self.name = name
        self.capabilities = frozenset({ModelCapability.CHAT})
        self._fail_times = fail_times
        self._error = error
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise self._error or TransientError("boom")
        return ModelResponse(message=ChatMessage.assistant("ok"), model=self.name)


def _settings() -> Settings:
    return Settings(environment="test", max_tool_retries=2)


def _request() -> ModelRequest:
    return ModelRequest(messages=(ChatMessage.user("hi"),))


def test_model_id_requires_provider_prefix():
    with pytest.raises(ConfigurationError):
        ProviderRegistry.split_model_id("gpt-4o-mini")


def test_gateway_rejects_unroutable_default_model():
    registry = ProviderRegistry()
    registry.register(_Provider("a"))
    with pytest.raises(Exception):
        ModelGateway(registry, settings=_settings(), default_model="b/model")


async def test_transient_error_is_retried_then_succeeds():
    registry = ProviderRegistry()
    provider = _Provider("a", fail_times=1)
    registry.register(provider)
    gateway = ModelGateway(registry, settings=_settings(), default_model="a/m")
    response = await gateway.complete(_request())
    assert response.content == "ok"
    assert provider.calls == 2
    assert gateway.stats.retries == 1


async def test_non_retryable_error_propagates_immediately():
    registry = ProviderRegistry()
    provider = _Provider("a", fail_times=1, error=ValidationError("bad input"))
    registry.register(provider)
    gateway = ModelGateway(registry, settings=_settings(), default_model="a/m")
    with pytest.raises(ValidationError):
        await gateway.complete(_request())
    assert provider.calls == 1


async def test_fallback_is_used_when_primary_fails():
    registry = ProviderRegistry()
    primary = _Provider("a", fail_times=99)
    backup = _Provider("b")
    registry.register(primary)
    registry.register(backup)
    gateway = ModelGateway(registry, settings=_settings(), default_model="a/m", fallbacks=("b/m",))
    response = await gateway.complete(_request())
    assert response.model == "b"
    assert backup.calls == 1


async def test_unsupported_capability_is_rejected():
    registry = ProviderRegistry()
    registry.register(_Provider("a"))
    gateway = ModelGateway(registry, settings=_settings(), default_model="a/m")
    with pytest.raises(ExternalServiceError):
        await gateway.complete(_request(), capability=ModelCapability.VISION)


async def test_usage_is_accounted():
    registry = ProviderRegistry()
    registry.register(_Provider("a"))
    gateway = ModelGateway(registry, settings=_settings(), default_model="a/m")
    await gateway.complete(_request())
    assert gateway.stats.requests == 1
    assert gateway.stats.per_model["a/m"] == 1
