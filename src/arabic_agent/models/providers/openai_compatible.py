"""OpenAI-compatible chat provider.

Works against any endpoint exposing ``POST /chat/completions`` with the OpenAI
schema — OpenAI itself, Azure OpenAI, Ollama (``/v1``), vLLM and llama.cpp.
Using httpx directly (rather than a vendor SDK) keeps the dependency surface
small and makes the local/offline story a matter of changing a base URL.
"""

from __future__ import annotations

import json
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


class OpenAICompatibleProvider(ModelProvider):
    """A provider for OpenAI-schema chat completion endpoints."""

    capabilities = frozenset(
        {ModelCapability.CHAT, ModelCapability.TOOL_CALLING, ModelCapability.CODE}
    )

    def __init__(
        self,
        *,
        name: str = "openai",
        base_url: str = "https://api.openai.com/v1",
        api_key: str | None = None,
        timeout: float = 60.0,
        client: httpx.AsyncClient | None = None,
        default_headers: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout
        self._default_headers = default_headers or {}
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", **self._default_headers}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    @staticmethod
    def _encode_message(message: ChatMessage) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": message.role, "content": message.content}
        if message.images:
            # OpenAI-compatible multimodal content array: a text part plus one
            # image_url part per attached image (a data URI, so no external
            # fetch and no local path leaks to the provider).
            parts: list[dict[str, Any]] = []
            if message.content:
                parts.append({"type": "text", "text": message.content})
            for image in message.images:
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.mime_type};base64,{image.data_base64}"},
                    }
                )
            payload["content"] = parts
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in message.tool_calls
            ]
            if not message.content:
                payload["content"] = None
        if message.tool_call_id is not None:
            payload["tool_call_id"] = message.tool_call_id
        if message.name is not None:
            payload["name"] = message.name
        return payload

    @staticmethod
    def _encode_tool(tool: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters or {"type": "object", "properties": {}},
            },
        }

    def _build_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model or "",
            "messages": [self._encode_message(m) for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = [self._encode_tool(t) for t in request.tools]
            payload["tool_choice"] = "auto"
        payload.update(request.extra)
        return payload

    @staticmethod
    def _parse_tool_calls(raw: Any) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        if not isinstance(raw, list):
            return ()
        for item in raw:
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            arguments_raw = function.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                )
            except json.JSONDecodeError:
                # Preserve the raw payload so the agent can attempt a repair
                # rather than silently dropping the call.
                arguments = {"_raw": arguments_raw}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{len(calls)}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        return tuple(calls)

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if not request.messages:
            raise ValidationError("model request requires at least one message")
        payload = self._build_payload(request)
        client = await self._get_client()
        url = f"{self._base_url}/chat/completions"
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
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ExternalServiceError("model provider returned no choices")
        choice = choices[0]
        message_raw = choice.get("message") or {}
        content = message_raw.get("content") or ""
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        tool_calls = self._parse_tool_calls(message_raw.get("tool_calls"))
        message = ChatMessage.assistant(content=content, tool_calls=tool_calls)
        usage_raw = data.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        return ModelResponse(
            message=message,
            usage=usage,
            model=str(data.get("model") or request.model or ""),
            finish_reason=choice.get("finish_reason"),
        )

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
