"""Local inference backends.

A backend is the thin, replaceable layer that actually runs a model on this
machine. The rest of the platform only sees the provider-neutral
:class:`~arabic_agent.models.base.ModelResponse`, so swapping llama.cpp for
Ollama (or a future engine) changes nothing in the agent core.

Two backends are provided:

* :class:`LlamaCppBackend` — in-process inference via the ``llama-cpp-python``
  bindings. This is the default: one Python process, no daemon, CPU or GPU,
  and identical on Windows, macOS and Linux.
* :class:`OllamaBackend` — delegates to a locally-running Ollama server over
  its OpenAI-compatible endpoint. No model file handling; useful when the user
  already runs Ollama.

Neither backend ever reaches the internet on its own. The Ollama backend talks
only to the configured loopback endpoint; the llama.cpp backend touches only the
local model file.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..core.errors import (
    ExternalServiceError,
    ResourceExhaustedError,
    TimeoutError_,
    ValidationError,
)
from ..models.base import (
    ChatMessage,
    ModelRequest,
    ModelResponse,
    ModelStreamDelta,
    ToolCall,
    Usage,
)

logger = logging.getLogger(__name__)

#: Qwen-family models (and several others) emit tool calls as a JSON object
#: inside ``<tool_call>...</tool_call>`` in the *content* rather than a
#: structured ``tool_calls`` field. We parse both shapes.
_OPEN_TAG = "<tool_call>"
_CLOSE_TAG = "</tool_call>"


def _estimate_message_tokens(message: ChatMessage) -> int:
    """Cheap, dependency-free token estimate for one message.

    Uses the same ~4-characters-per-token heuristic the context manager uses,
    so the guard and the budget speak the same units. It only needs to be good
    enough to refuse an obviously oversized request before it reaches ggml.
    """
    length = len(message.content or "")
    for call in message.tool_calls or ():
        length += len(call.name) + len(json.dumps(call.arguments, ensure_ascii=False))
    return length // 4 + 4  # per-message overhead


def _extract_balanced_json(text: str, start: int) -> int | None:
    """Return the index just past the JSON object starting at ``start``.

    Scans with string/escape awareness so nested objects (tool arguments
    frequently nest) do not terminate the object early. Returns ``None`` when
    no balanced object is present yet — which, during streaming, simply means
    more text is needed.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _loads_tolerant(raw: str) -> Any:
    """Parse a tool-call JSON object, tolerating a stray extra brace.

    Some small quantised models (observed with Qwen2.5-0.5B-Q4) emit an extra
    ``{`` before the tool object, producing ``{{"name": ...}}`` — which is not
    balanced JSON. We try a bounded set of deterministic repairs (drop one
    leading and/or one trailing brace) and accept only a result that parses to
    a JSON object. Nothing is fabricated: if no repair parses, return ``None``
    and the caller drops that block.
    """
    candidates = [raw]
    stripped = raw.strip()
    if stripped.startswith("{{"):
        candidates.append(stripped[1:])
    if stripped.endswith("}}"):
        candidates.append(stripped[:-1])
    if stripped.startswith("{{") and stripped.endswith("}}"):
        candidates.append(stripped[1:-1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def parse_tool_calls(content: str) -> tuple[tuple[ToolCall, ...], str]:
    """Extract tool calls from model content, returning ``(calls, remaining)``.

    Handles the ``<tool_call>{...}</tool_call>`` convention used by Qwen and
    other locally-run models. The JSON body is taken as the text between the
    tags and parsed tolerantly; a malformed block is dropped (never fabricated)
    and the agent recovers, while genuinely incomplete blocks are left in
    ``remaining`` for a later chunk.
    """
    calls: list[ToolCall] = []
    remaining = content
    while True:
        open_at = remaining.find(_OPEN_TAG)
        if open_at == -1:
            break
        json_at = open_at + len(_OPEN_TAG)
        close_at = remaining.find(_CLOSE_TAG, json_at)
        if close_at == -1:
            break  # block not closed yet: leave it for the caller
        raw = remaining[json_at:close_at].strip()
        payload = _loads_tolerant(raw) if raw else None
        if isinstance(payload, dict):
            name = payload.get("name") or payload.get("function")
            arguments = payload.get("arguments", payload.get("parameters", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"_raw": arguments}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            if isinstance(name, str) and name:
                calls.append(ToolCall(id=f"call_{len(calls)}", name=name, arguments=arguments))
        remaining = remaining[:open_at] + remaining[close_at + len(_CLOSE_TAG) :]
    return tuple(calls), remaining.strip()


def _split_streamable(buffer: str) -> tuple[str, str]:
    """Split ``buffer`` into text safe to emit now and a held-back suffix.

    While a model is streaming, any text at or after ``<tool_call>`` cannot be
    emitted until the block closes (or the stream ends and it is parsed). Text
    before the tag is safe; the tag and everything after it are withheld.
    """
    open_at = buffer.find(_OPEN_TAG)
    if open_at != -1:
        return buffer[:open_at], buffer[open_at:]
    # No tag yet, but the tail may be the beginning of one ("<tool", "<tool_c",
    # …). Withhold the longest such partial suffix until the next chunk.
    for length in range(min(len(_OPEN_TAG) - 1, len(buffer)), 0, -1):
        if _OPEN_TAG.startswith(buffer[-length:]):
            return buffer[:-length], buffer[-length:]
    return buffer, ""


@runtime_checkable
class LocalInferenceBackend(Protocol):
    """The contract a local engine implements."""

    name: str

    async def load(self) -> None:
        """Load the model (idempotent). Raises on failure."""

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        """Run one completion, honouring ``timeout``."""

    def stream(
        self, request: ModelRequest, *, timeout_seconds: float
    ) -> AsyncIterator[ModelStreamDelta]:
        """Stream a completion (optional; may raise NotImplementedError)."""

    async def healthy(self) -> bool:
        """Return whether the backend is loaded and can serve a request."""

    async def close(self) -> None:
        """Release the model and any resources."""

    def model_info(self) -> dict[str, Any]:
        """Backend-reported metadata (context size, etc.), never invented."""


class LlamaCppBackend:
    """In-process inference with ``llama-cpp-python``.

    The model is loaded lazily on first use so constructing the platform does
    not block on a multi-hundred-megabyte file. Inference runs in a worker
    thread (the binding is synchronous); a lock serialises calls because a
    single ``Llama`` instance is not safe for concurrent generation.
    """

    name = "llama_cpp"

    def __init__(
        self,
        *,
        model_path: str,
        context_window: int = 4096,
        n_threads: int | None = None,
        n_gpu_layers: int = 0,
        chat_format: str | None = None,
        verbose: bool = False,
        extra_load_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._model_path = model_path
        self._context_window = context_window
        self._n_threads = n_threads
        self._n_gpu_layers = n_gpu_layers
        self._chat_format = chat_format
        self._verbose = verbose
        self._extra_load_kwargs = dict(extra_load_kwargs or {})
        self._llama: Any = None
        self._lock = asyncio.Lock()
        self._load_lock = asyncio.Lock()

    async def load(self) -> None:
        async with self._load_lock:
            if self._llama is not None:
                return
            path = Path(self._model_path)
            if not await asyncio.to_thread(path.is_file):
                raise ValidationError(f"local model file not found: {path}")
            try:
                from llama_cpp import Llama  # imported lazily: optional dependency
            except ImportError as exc:  # pragma: no cover - depends on install
                raise ResourceExhaustedError(
                    "llama-cpp-python is not installed; install it to use a local model",
                    details={"backend": self.name},
                ) from exc
            kwargs: dict[str, Any] = {
                "model_path": str(path),
                "n_ctx": self._context_window,
                "n_gpu_layers": self._n_gpu_layers,
                "verbose": self._verbose,
            }
            if self._n_threads:
                kwargs["n_threads"] = self._n_threads
            if self._chat_format:
                kwargs["chat_format"] = self._chat_format
            kwargs.update(self._extra_load_kwargs)
            try:
                self._llama = await asyncio.to_thread(Llama, **kwargs)
            except Exception as exc:  # surface as a platform error
                raise ExternalServiceError(
                    f"failed to load local model: {exc}", details={"path": str(path)}
                ) from exc
            logger.info("local model loaded: %s", path.name)

    def _guard_context(self, request: ModelRequest) -> None:
        """Refuse a request whose prompt cannot fit the model's context.

        The conversation token budget is a platform setting; if it is larger
        than the local model's ``n_ctx`` we would hand llama.cpp a prompt it
        must silently truncate (or, in observed builds, crash on). Better to
        fail closed with a clear error the caller can act on.
        """
        if self._llama is None or not request.messages:
            return
        prompt_tokens = 0
        for message in request.messages:
            prompt_tokens += _estimate_message_tokens(message)
        if request.tools:
            prompt_tokens += (
                sum(len(tool.name) + len(tool.description) for tool in request.tools) // 4
            )
        # Always keep headroom for generation. A prompt that fills the window
        # leaves the sampler with no logits, which aborts inside ggml; fail
        # closed instead.
        reserved = (
            request.max_tokens
            if request.max_tokens is not None
            else max(64, self._context_window // 8)
        )
        if prompt_tokens + reserved >= self._context_window:
            raise ValidationError(
                "local model request exceeds the model context window",
                details={
                    "estimated_prompt_tokens": prompt_tokens,
                    "reserved_output_tokens": reserved,
                    "context_window": self._context_window,
                },
            )

    def _messages_payload(self, request: ModelRequest) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for message in request.messages:
            entry: dict[str, Any] = {"role": message.role, "content": message.content}
            if message.tool_calls:
                entry["tool_calls"] = [
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
            if message.tool_call_id is not None:
                entry["tool_call_id"] = message.tool_call_id
            if message.name is not None:
                entry["name"] = message.name
            payload.append(entry)
        return payload

    def _tools_payload(self, request: ModelRequest) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters or {"type": "object", "properties": {}},
                },
            }
            for tool in request.tools
        ]

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        if not request.messages:
            raise ValidationError("model request requires at least one message")
        await self.load()
        assert self._llama is not None
        self._guard_context(request)
        payload: dict[str, Any] = {
            "messages": self._messages_payload(request),
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = self._tools_payload(request)
            payload["tool_choice"] = "auto"
        payload.update(request.extra)

        async with self._lock:
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self._llama.create_chat_completion, **payload),
                    timeout=timeout_seconds,
                )
            except TimeoutError as exc:
                raise TimeoutError_(
                    f"local model exceeded its {timeout_seconds:.0f}s deadline",
                    details={"backend": self.name},
                ) from exc
            except ValueError as exc:
                # llama.cpp raises ValueError for context overflow and bad input.
                raise ValidationError(f"local model rejected the request: {exc}") from exc
            except Exception as exc:
                raise ExternalServiceError(f"local model failed: {exc}") from exc
        return self._to_response(result, request)

    async def stream(
        self, request: ModelRequest, *, timeout_seconds: float
    ) -> AsyncIterator[ModelStreamDelta]:
        if not request.messages:
            raise ValidationError("model request requires at least one message")
        await self.load()
        assert self._llama is not None
        self._guard_context(request)
        payload: dict[str, Any] = {
            "messages": self._messages_payload(request),
            "temperature": request.temperature,
            "stream": True,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.tools:
            payload["tools"] = self._tools_payload(request)
            payload["tool_choice"] = "auto"
        payload.update(request.extra)

        async with self._lock:
            iterator = await asyncio.to_thread(
                lambda: self._llama.create_chat_completion(**payload)
            )
            # Qwen-family models stream a tool call as ``<tool_call>{...}``
            # fragments inside content, so text cannot be emitted verbatim:
            # hold back any text that could be the start of a block and only
            # flush what is provably outside one.
            buffered = ""
            async for delta in self._iter_chunks(iterator):
                buffered += delta.content
                emitted, buffered = _split_streamable(buffered)
                if emitted or delta.tool_calls or delta.finish_reason is not None:
                    yield ModelStreamDelta(
                        content=emitted,
                        tool_calls=delta.tool_calls,
                        finish_reason=delta.finish_reason,
                    )
            # A complete block left in the buffer is a tool call, not content.
            calls, leftover = parse_tool_calls(buffered)
            if leftover:
                yield ModelStreamDelta(content=leftover)
            if calls:
                yield ModelStreamDelta(tool_calls=calls, finish_reason="tool_calls")

    @staticmethod
    async def _iter_chunks(iterator: Any) -> AsyncIterator[ModelStreamDelta]:
        while True:
            chunk = await asyncio.to_thread(next, iterator, None)
            if chunk is None:
                break
            choice = chunk.get("choices", [{}])[0]
            delta = choice.get("delta", {}) or {}
            yield ModelStreamDelta(
                content=delta.get("content") or "",
                finish_reason=choice.get("finish_reason"),
            )

    def _to_response(self, result: dict[str, Any], request: ModelRequest) -> ModelResponse:
        choices = result.get("choices") or []
        if not choices:
            raise ExternalServiceError("local model returned no choices")
        choice = choices[0]
        message_raw = choice.get("message") or {}
        content = message_raw.get("content") or ""
        structured = message_raw.get("tool_calls")
        tool_calls: tuple[ToolCall, ...] = ()
        if isinstance(structured, list) and structured:
            tool_calls = self._parse_structured_tool_calls(structured)
        if not tool_calls and content:
            tool_calls, content = parse_tool_calls(content)
        usage_raw = result.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )
        message = ChatMessage.assistant(content=content, tool_calls=tool_calls)
        return ModelResponse(
            message=message,
            usage=usage,
            model=request.model or Path(self._model_path).name,
            finish_reason=choice.get("finish_reason"),
        )

    @staticmethod
    def _parse_structured_tool_calls(raw: list[Any]) -> tuple[ToolCall, ...]:
        calls: list[ToolCall] = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            function = item.get("function") or {}
            arguments_raw = function.get("arguments") or "{}"
            try:
                arguments = (
                    json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                )
            except json.JSONDecodeError:
                arguments = {"_raw": arguments_raw}
            if not isinstance(arguments, dict):
                arguments = {"_raw": arguments}
            calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{index}"),
                    name=str(function.get("name") or ""),
                    arguments=arguments,
                )
            )
        return tuple(calls)

    async def healthy(self) -> bool:
        if self._llama is None:
            return False
        return await asyncio.to_thread(Path(self._model_path).is_file)

    async def close(self) -> None:
        self._llama = None

    def model_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {"backend": self.name, "model_path": self._model_path}
        if self._llama is not None:
            meta = getattr(self._llama, "metadata", {}) or {}
            info["chat_format"] = getattr(self._llama, "chat_format", None)
            for key in ("general.name", "general.architecture", "general.file_type"):
                if key in meta:
                    info[key] = meta[key]
            n_ctx = meta.get(f"{meta.get('general.architecture', '')}.context_length")
            if isinstance(n_ctx, int):
                info["native_context_length"] = n_ctx
        return info


class OllamaBackend:
    """Delegates inference to a local Ollama server (OpenAI-compatible)."""

    name = "ollama"

    def __init__(
        self,
        *,
        model: str,
        base_url: str = "http://127.0.0.1:11434/v1",
        timeout: float = 120.0,
    ) -> None:
        from ..models.providers.openai_compatible import OpenAICompatibleProvider

        self._provider = OpenAICompatibleProvider(
            name="local", base_url=base_url, api_key=None, timeout=timeout
        )
        self._model = model
        self._base_url = base_url

    async def load(self) -> None:
        return None

    async def generate(self, request: ModelRequest, *, timeout_seconds: float) -> ModelResponse:
        routed = request.model_copy(update={"model": self._model})
        return await self._provider.complete(routed)

    async def stream(
        self, request: ModelRequest, *, timeout_seconds: float
    ) -> AsyncIterator[ModelStreamDelta]:
        routed = request.model_copy(update={"model": self._model})
        async for delta in self._provider.stream(routed):
            yield delta

    async def healthy(self) -> bool:
        client = await self._provider._get_client()
        try:
            response = await client.get(f"{self._base_url.rstrip('/')}/models", timeout=5.0)
        except Exception:  # health check must not raise
            return False
        return response.status_code == 200

    async def close(self) -> None:
        await self._provider.aclose()

    def model_info(self) -> dict[str, Any]:
        return {"backend": self.name, "model": self._model, "base_url": self._base_url}
