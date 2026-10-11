"""Cloud model provider tests.

The providers are exercised against a *real* local HTTP server (the same
``fake_http`` harness the connector tests use), so request shaping, header
construction, response parsing and error classification are proven against real
HTTP behaviour — not a stubbed client. No test contacts a vendor endpoint.
"""

from __future__ import annotations

import json

import pytest

from arabic_agent.core.config import Settings
from arabic_agent.core.enums import ModelCapability
from arabic_agent.core.errors import ExternalServiceError, TransientError
from arabic_agent.models.base import ChatMessage, ModelRequest, ToolSpec
from arabic_agent.models.providers.anthropic_compatible import AnthropicCompatibleProvider
from arabic_agent.models.providers.openai_compatible import OpenAICompatibleProvider

from .fake_http import http_server


def _request() -> ModelRequest:
    return ModelRequest(
        messages=(
            ChatMessage.system("you are helpful"),
            ChatMessage.user("مرحبا"),
        ),
        tools=(ToolSpec(name="echo", description="echo", parameters={"type": "object"}),),
        model="claude-3-5-haiku-20241022",
        max_tokens=64,
    )


@pytest.mark.asyncio
async def test_anthropic_request_shape_and_headers() -> None:
    with http_server() as server:
        server.route(
            "/v1/messages",
            body=json.dumps(
                {
                    "model": "claude-3-5-haiku-20241022",
                    "content": [{"type": "text", "text": "أهلا"}],
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                    "stop_reason": "end_turn",
                }
            ),
        )
        provider = AnthropicCompatibleProvider(
            base_url=f"{server.base_url}/v1", api_key="secret-key"
        )
        response = await provider.complete(_request())
        await provider.aclose()

        assert response.content == "أهلا"
        assert response.usage.prompt_tokens == 7
        assert response.usage.completion_tokens == 3

        sent = server.requests[0]
        assert sent.path == "/v1/messages"
        assert sent.headers["x-api-key"] == "secret-key"
        assert sent.headers["anthropic-version"] == "2023-06-01"
        body = sent.json()
        # The system prompt is a top-level field, not a message.
        assert body["system"] == "you are helpful"
        assert all(m["role"] != "system" for m in body["messages"])
        assert body["max_tokens"] == 64
        assert body["tools"][0]["input_schema"] == {"type": "object"}


@pytest.mark.asyncio
async def test_anthropic_parses_tool_use_blocks() -> None:
    with http_server() as server:
        server.route(
            "/v1/messages",
            body=json.dumps(
                {
                    "content": [
                        {"type": "text", "text": "calling"},
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "echo",
                            "input": {"text": "hi"},
                        },
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            ),
        )
        provider = AnthropicCompatibleProvider(base_url=f"{server.base_url}/v1", api_key="k")
        response = await provider.complete(_request())
        await provider.aclose()
        assert response.content == "calling"
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "echo"
        assert response.tool_calls[0].arguments == {"text": "hi"}


@pytest.mark.asyncio
async def test_anthropic_rate_limit_is_transient() -> None:
    with http_server() as server:
        server.route("/v1/messages", status=429, body="{}")
        provider = AnthropicCompatibleProvider(base_url=f"{server.base_url}/v1", api_key="k")
        with pytest.raises(TransientError):
            await provider.complete(_request())
        await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_client_error_is_not_retryable() -> None:
    with http_server() as server:
        server.route("/v1/messages", status=400, body="{}")
        provider = AnthropicCompatibleProvider(base_url=f"{server.base_url}/v1", api_key="k")
        with pytest.raises(ExternalServiceError):
            await provider.complete(_request())
        await provider.aclose()


@pytest.mark.asyncio
async def test_anthropic_tool_result_encoding() -> None:
    with http_server() as server:
        server.route("/v1/messages", body=json.dumps({"content": [{"type": "text", "text": "ok"}]}))
        provider = AnthropicCompatibleProvider(base_url=f"{server.base_url}/v1", api_key="k")
        request = ModelRequest(
            messages=(ChatMessage.tool_result("toolu_1", "result text"),),
            model="claude-3-5-haiku-20241022",
        )
        await provider.complete(request)
        await provider.aclose()
        blocks = server.requests[0].json()["messages"][0]["content"]
        assert blocks[0]["type"] == "tool_result"
        assert blocks[0]["tool_use_id"] == "toolu_1"


@pytest.mark.asyncio
async def test_openai_provider_uses_configured_base_url() -> None:
    with http_server() as server:
        server.route(
            "/v1/chat/completions",
            body=json.dumps(
                {
                    "choices": [{"message": {"role": "assistant", "content": "hi"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }
            ),
        )
        provider = OpenAICompatibleProvider(base_url=f"{server.base_url}/v1", api_key="k")
        response = await provider.complete(
            ModelRequest(messages=(ChatMessage.user("hi"),), model="gpt-4o-mini")
        )
        await provider.aclose()
        assert response.content == "hi"
        assert server.requests[0].path == "/v1/chat/completions"


def test_container_registers_anthropic_when_key_present() -> None:
    """The container wires the Anthropic provider only when a key is set."""
    from pydantic import SecretStr

    from arabic_agent.container import Platform

    settings = Settings(
        environment="test",
        anthropic_api_key=SecretStr("test-key"),
        openai_api_key=SecretStr(""),
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        default_model="anthropic/claude-3-5-haiku-20241022",
    )
    platform = Platform.build(settings)
    names = platform.gateway._registry.names()
    assert "anthropic" in names
    assert platform.gateway.default_model == "anthropic/claude-3-5-haiku-20241022"


def test_anthropic_provider_is_remote() -> None:
    provider = AnthropicCompatibleProvider(api_key="k")
    assert provider.is_local is False
    assert ModelCapability.TOOL_CALLING in provider.capabilities
