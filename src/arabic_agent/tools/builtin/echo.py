"""Utility tools: echo (for testing/observability) and finish (to end a run)."""

from __future__ import annotations

from typing import Any, ClassVar

from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult


class EchoTool(Tool):
    """Returns its input unchanged. Requires no permissions.

    Useful for connectivity checks, tests, and letting the agent produce a
    structured observation without side effects.
    """

    name = "echo"
    description = "Return the provided text unchanged. Requires no permissions."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to echo back"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    permissions: tuple[Permission, ...] = ()

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        text = arguments["text"]
        return ToolResult.success(f"echoed {len(text)} characters", data={"text": text})


class FinishTool(Tool):
    """Signals that the agent considers the goal achieved.

    The agent uses this to return a structured final result, including a
    self-assessment, rather than ending on an arbitrary assistant message.
    """

    name = "finish"
    description = (
        "Declare the task complete and return the final result to the user. "
        "Call this only after verifying the outcome."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "result": {"type": "string", "description": "Final result for the user"},
            "verified": {
                "type": "boolean",
                "description": "Whether the outcome was verified against the goal",
            },
        },
        "required": ["result"],
        "additionalProperties": False,
    }
    permissions: tuple[Permission, ...] = ()

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        return ToolResult.success(
            "run marked complete",
            data={
                "result": arguments["result"],
                "verified": bool(arguments.get("verified", False)),
            },
        )
