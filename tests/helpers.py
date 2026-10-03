"""Test doubles and helpers shared across the suite.

Tests never call a real model. :class:`FakeProvider` implements the provider
contract deterministically, so the *real* agent loop, executor, policy engine,
task engine, context manager and tools are exercised end to end — only the LLM
is substituted.
"""

from __future__ import annotations

import json
from collections.abc import Callable

from arabic_agent.core.enums import ModelCapability
from arabic_agent.models.base import (
    ChatMessage,
    ModelProvider,
    ModelRequest,
    ModelResponse,
    ToolCall,
    Usage,
)

Responder = Callable[[ModelRequest, int], ModelResponse]


class FakeProvider(ModelProvider):
    """A deterministic provider driven by a responder callable."""

    name = "fake"
    capabilities = frozenset({ModelCapability.CHAT, ModelCapability.TOOL_CALLING})

    def __init__(self, respond: Responder) -> None:
        self._respond = respond
        self.calls: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls.append(request)
        return self._respond(request, len(self.calls))


def text_response(content: str) -> ModelResponse:
    return ModelResponse(message=ChatMessage.assistant(content), model="fake")


def tool_response(name: str, arguments: dict, *, call_id: str = "call_1") -> ModelResponse:
    return ModelResponse(
        message=ChatMessage.assistant(
            "", tool_calls=(ToolCall(id=call_id, name=name, arguments=arguments),)
        ),
        model="fake",
        usage=Usage(prompt_tokens=10, completion_tokens=5),
    )


def plan_json(*steps: tuple[str, str | None]) -> str:
    return json.dumps({"steps": [{"description": d, "tool_hint": h} for d, h in steps]})


def verify_json(verified: bool, result: str) -> str:
    return json.dumps({"verified": verified, "result": result})


def is_verifier(request: ModelRequest) -> bool:
    return any(m.role == "system" and "strict verifier" in m.content for m in request.messages)


def is_planner(request: ModelRequest) -> bool:
    return not request.tools and not is_verifier(request)


def scripted_responder(
    *,
    plan: str,
    tool_calls: list[tuple[str, dict]],
    verification: str | None = None,
) -> Responder:
    """Build a responder: planner → tool calls (in order) → verification."""
    state = {"tool_index": 0}

    def respond(request: ModelRequest, _n: int) -> ModelResponse:
        if is_verifier(request):
            return text_response(verification or verify_json(True, "done"))
        if is_planner(request):
            return text_response(plan)
        index = state["tool_index"]
        state["tool_index"] = index + 1
        if index < len(tool_calls):
            name, arguments = tool_calls[index]
            return tool_response(name, arguments, call_id=f"call_{index}")
        return text_response("no further action")

    return respond
