"""Tool system: the extensible action surface of the platform.

Tools are plugins. They declare a JSON-schema, the permissions they need, and a
handler. The agent discovers and invokes them through a registry; the core never
hard-codes a tool.
"""

from .base import Tool, ToolContext, ToolResult
from .registry import ToolRegistry

__all__ = ["Tool", "ToolContext", "ToolRegistry", "ToolResult"]
