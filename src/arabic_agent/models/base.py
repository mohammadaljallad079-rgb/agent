"""Model-layer data contracts.

These types are the *only* thing the agent core knows about models. Providers
translate between these types and whatever wire format their vendor uses, so
swapping a cloud model for a local one never touches the agent loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ModelCapability

Role = Literal["system", "user", "assistant", "tool"]


class ToolSpec(BaseModel):
    """A tool as advertised to a model (JSON-schema style)."""

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    """A model's request to invoke a tool."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(BaseModel):
    """A single message in a conversation, provider-neutral."""

    model_config = ConfigDict(frozen=True)

    role: Role
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None

    @classmethod
    def system(cls, content: str) -> ChatMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> ChatMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str = "", tool_calls: tuple[ToolCall, ...] = ()) -> ChatMessage:
        return cls(role="assistant", content=content, tool_calls=tool_calls)

    @classmethod
    def tool_result(cls, tool_call_id: str, content: str, name: str | None = None) -> ChatMessage:
        return cls(role="tool", content=content, tool_call_id=tool_call_id, name=name)


class Usage(BaseModel):
    """Token accounting for cost and context budgeting."""

    model_config = ConfigDict(frozen=True)

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Usage) -> Usage:
        if not isinstance(other, Usage):
            return NotImplemented
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )

    def __radd__(self, other: object) -> Usage:
        if other == 0:  # allows sum() over an empty sequence
            return self
        if isinstance(other, Usage):
            return self.__add__(other)
        return NotImplemented


class ModelRequest(BaseModel):
    """A provider-neutral completion request."""

    model_config = ConfigDict(frozen=True)

    messages: tuple[ChatMessage, ...]
    tools: tuple[ToolSpec, ...] = ()
    model: str | None = None
    temperature: float = 0.0
    max_tokens: int | None = None
    # Opaque, provider-specific knobs (e.g. top_p, reasoning effort).
    extra: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    """A provider-neutral completion response."""

    model_config = ConfigDict(frozen=True)

    message: ChatMessage
    usage: Usage = Field(default_factory=Usage)
    model: str = ""
    finish_reason: str | None = None

    @property
    def tool_calls(self) -> tuple[ToolCall, ...]:
        return self.message.tool_calls

    @property
    def content(self) -> str:
        return self.message.content


class ModelStreamDelta(BaseModel):
    """An incremental chunk of a streamed response."""

    model_config = ConfigDict(frozen=True)

    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None


class ModelProvider(ABC):
    """Contract every model provider implements.

    A provider may be cloud (OpenAI/Anthropic/Gemini) or local (Ollama, vLLM,
    llama.cpp). Nothing in the agent core may depend on which.
    """

    #: Stable identifier used in routing config (e.g. "openai").
    name: str
    #: Capabilities this provider/model family can serve.
    capabilities: frozenset[ModelCapability] = frozenset({ModelCapability.CHAT})
    #: Whether this provider runs on the user's machine. Cloud providers leave
    #: this ``False``; the local runtime sets it ``True``. Used for cost
    #: accounting and routing without importing a concrete provider.
    is_local: bool = False

    @abstractmethod
    async def complete(self, request: ModelRequest) -> ModelResponse:
        """Run a single completion. Must not raise bare exceptions.

        Providers translate vendor failures into :class:`PlatformError`
        subclasses (e.g. ``TransientError`` for rate limits).
        """

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamDelta]:
        """Stream a completion.

        The default implementation degrades gracefully to a single delta so
        providers that cannot stream still satisfy the interface.
        """
        response = await self.complete(request)
        yield ModelStreamDelta(
            content=response.content,
            tool_calls=response.tool_calls,
            finish_reason=response.finish_reason,
        )

    async def aclose(self) -> None:
        """Release any held resources (HTTP clients, sockets)."""
        return None

    def supports(self, capability: ModelCapability) -> bool:
        return capability in self.capabilities
