"""Anthropic-compatible chat provider.

Speaks the Anthropic Messages API (``POST /v1/messages``). Anthropic's schema
differs from OpenAI's in three ways this adapter bridges:

* the system prompt is a top-level ``system`` field, not a message;
* content is a list of typed blocks (``text``, ``tool_use``, ``tool_result``);
* tool calls are ``tool_use`` blocks and results are ``tool_result`` blocks
  inside a user message.

Using httpx directly (rather than a vendor SDK) keeps the dependency surface
small, exactly as the OpenAI-compatible provider does. The provider is not
registered unless an operator configures a key, so it never reaches the network
on its own.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...core.enums import ModelCapability
from ...core.errors import ExternalServiceError, TransientError, ValidationError
from ..base import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ToolCall,
    ToolSpec,
    Usage,
)

logger = logging.getLogger(__name__)

#: Anthropic requires a max token count on every request.
_DEFAULT_MAX_TOKENS = 1024


class AnthropicCompatibleProvider(ModelProvider):
    """A provider for the Anthropic Messages API."""

    capabilities = frozenset(
        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
    )

    def __init__(
        self,
        *,
        name: str = "anthropic",
        base_url: str = "https://api.anthropic.com/v1",
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        anthropic_version: str = "2023-06-01",
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None
        self._version = anthropic_version

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-api-key": self._api_key,
            "anthropic-version": self._version,
        }

    @staticmethod
    def _content_blocks(message: ChatMessage) -> list[dict[str, Any]]:
        """Encode one message's content as Anthropic content blocks."""
        blocks: list[dict[str, Any]] = []
        if message.content:
            blocks.append({"type": "text", "text": message.content})
        for call in message.tool_calls or ():
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": call.arguments,
                }
            )
        if message.tool_call_id is not None:
            # A tool result is a user-turn content block.
            blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": message.tool_call_id,
                    "content": message.content or "",
                }
            )
            if message.content:
                # The text was already added above; drop the duplicate text block.
                blocks = [b for b in blocks if b.get("type") != "text"]
        return blocks or [{"type": "text", "text": ""}]

    @staticmethod
    def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters or {"type": "object", "properties": {}},
        }

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            role = "assistant" if message.role == "assistant" else "user"
            messages.append({"role": role, "content": self._content_blocks(message)})
        payload: dict[str, Any] = {
            "model": request.model or "",
            "messages": messages,
            "max_tokens": request.max_tokens or _DEFAULT_MAX_TOKENS,
            "temperature": request.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.tools:
            payload["tools"] = [self._encode_tool(t) for t in request.tools]
        payload.update(request.extra)
        return payload

    @staticmethod
    def _parse_tool_calls(blocks: list[Any]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            arguments = block.get("input")
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            calls.append(
                ToolCall(
                    id=str(block.get("id") or f"call_{len(calls)}"),
                    name=str(block.get("name") or ""),
                    arguments=arguments,
                )
            )
        return tuple(calls)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ValidationError("model request requires at least one message")
        payload = self._build_payload(request)
        client = await self._get_client()
        url = f"{self._base_url}/messages"
        try:
            response = await client.post(url, headers=self._headers(), json=payload)
        except httpx.TimeoutException as exc:
            raise TransientError(
                f"model request timed out after {self._timeout}s", details={"url": url}
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientError(
                f"model request transport error: {exc}", details={"url": url}
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise TransientError(
                f"model provider returned {response.status_code}",
                details={"status": response.status_code, "body": response.text[:500]},
            )
        if response.status_code >= 400:
            raise ExternalServiceError(
                f"model provider rejected request: {response.status_code}",
                details={"status": response.status_code, "body": response.text[:500]},
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ExternalServiceError("model provider returned non-JSON body") from exc
        return self._parse_response(data, request)

    def _parse_response(self, data: dict[str, Any], request: ModelRequest) -> ModelResponse:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ExternalServiceError("model provider returned no content blocks")
        text_parts = [
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        tool_calls = self._parse_tool_calls(blocks)
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("input_tokens") or 0),
            completion_tokens=int(usage_raw.get("output_tokens") or 0),
        )
        return ModelResponse(
            message=ChatMessage.assistant("".join(text_parts), tool_calls=tool_calls),
            model=str(data.get("model") or request.model or self.name),
            usage=usage,
        )

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None


__all__ = ["AnthropicCompatibleProvider"]
