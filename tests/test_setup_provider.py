"""First-run setup provider and gateway default-model switching.

These cover two honest-failure surfaces of the model layer:

* a fresh install with nothing configured must still start, register the
  setup-required provider, and refuse inference with an actionable error
  (never fabricate a completion);
* the gateway's default model can be switched at runtime (as the desktop does
  when a local model is activated) without rebuilding the platform.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ModelCapability
from arabic_agent.core.errors import ConfigurationError, NotFoundError
from arabic_agent.models.base import ChatMessage, ModelProvider, ModelRequest, ModelResponse
from arabic_agent.models.gateway import ModelGateway
from arabic_agent.models.providers.setup_required import SETUP_MODEL_ID, SetupRequiredProvider
from arabic_agent.models.registry import ProviderRegistry


def _bare_settings() -> Settings:
    return Settings(
        environment="test",
        openai_api_key=SecretStr(""),
        anthropic_api_key=SecretStr(""),
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        default_model="",
    )


def test_fresh_install_registers_setup_provider_and_boots() -> None:
    """No key, no local weights: the platform still assembles, honestly."""
    platform = Platform.build(_bare_settings())
    assert platform.gateway.default_model == SETUP_MODEL_ID
    names = platform.gateway.registry.names()
    assert "setup" in names


async def test_setup_provider_refuses_with_actionable_error() -> None:
    """The setup provider never fabricates output; it names the next step."""
    provider = SetupRequiredProvider()
    request = ModelRequest(messages=(ChatMessage.user("مرحبا"),))
    with pytest.raises(ConfigurationError) as excinfo:
        await provider.complete(request)
    assert excinfo.value.details.get("setup_required") is True
    assert "install" in str(excinfo.value)


async def test_task_against_setup_provider_fails_not_succeeds() -> None:
    """A gateway call in setup mode raises; it cannot silently succeed."""
    platform = Platform.build(_bare_settings())
    with pytest.raises(ConfigurationError):
        await platform.gateway.complete(ModelRequest(messages=(ChatMessage.user("اعمل تقرير"),)))


class _Switchable(ModelProvider):
    def __init__(self, name: str) -> None:
        self.name = name
        self.capabilities = frozenset({ModelCapability.CHAT})

    async def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(message=ChatMessage.assistant(self.name), model=self.name)


def test_gateway_switches_default_model_without_rebuild() -> None:
    """Activation switches the primary and demotes the old default to fallback."""
    registry = ProviderRegistry()
    registry.register(_Switchable("a"))
    registry.register(_Switchable("b"))
    gateway = ModelGateway(registry, settings=Settings(environment="test"), default_model="a/one")
    assert gateway.default_model == "a/one"

    gateway.set_default_model("b/two")
    assert gateway.default_model == "b/two"
    assert "a/one" in gateway.fallbacks
    assert gateway.fallbacks[0] == "a/one"


async def test_gateway_routes_to_switched_default() -> None:
    """After switching, completions are served by the new default model."""
    registry = ProviderRegistry()
    registry.register(_Switchable("a"))
    registry.register(_Switchable("b"))
    gateway = ModelGateway(registry, settings=Settings(environment="test"), default_model="a/one")
    gateway.set_default_model("b/two")

    response = await gateway.complete(ModelRequest(messages=(ChatMessage.user("hi"),)))
    assert response.model == "b"


def test_gateway_rejects_unroutable_switch() -> None:
    """Switching to a model with no registered provider is refused."""
    registry = ProviderRegistry()
    registry.register(_Switchable("a"))
    gateway = ModelGateway(registry, settings=Settings(environment="test"), default_model="a/one")
    with pytest.raises(NotFoundError):
        gateway.set_default_model("ghost/model")
    assert gateway.default_model == "a/one"
