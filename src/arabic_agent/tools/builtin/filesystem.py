"""Filesystem tools: read, write and list, sandboxed to the workspace."""

from __future__ import annotations

from typing import Any, ClassVar

from ...core.errors import NotFoundError, ValidationError
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult
from ._fileops import read_text_file
from ._paths import resolve_workspace_path

#: Maximum bytes returned to the model in one read, to protect context budget.
MAX_READ_BYTES = 64 * 1024


class ReadFileTool(Tool):
    name = "read_file"
    description = (
        "Read a UTF-8 text file from the workspace. Returns at most "
        f"{MAX_READ_BYTES} bytes; the path must be relative to the workspace."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.READ_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = resolve_workspace_path(context.workspace_dir, arguments["path"], must_exist=True)
        if not path.is_file():
            raise ValidationError("path is not a regular file", details={"path": arguments["path"]})
        result = read_text_file(path, max_bytes=MAX_READ_BYTES)
        summary = f"read {len(result.text)} chars from {arguments['path']}"
        if result.truncated:
            summary += " (truncated)"
        return ToolResult.success(
            summary,
            data={
                "path": arguments["path"],
                "content": result.text,
                "truncated": result.truncated,
                "has_more": result.has_more,
                "encoding": result.encoding,
            },
        )


class WriteFileTool(Tool):
    name = "write_file"
    description = (
        "Write UTF-8 text to a file in the workspace, creating parent "
        "directories as needed. Overwrites existing content."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "UTF-8 text to write"},
            "append": {
                "type": "boolean",
                "description": "Append instead of overwriting (default false)",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)

    async def preview(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        path = resolve_workspace_path(context.workspace_dir, arguments["path"])
        content = str(arguments.get("content", ""))
        append = bool(arguments.get("append"))
        preview: dict[str, Any] = {
            "operation": "append_file" if append else "write_file",
            "path": arguments["path"],
            "bytes": len(content.encode("utf-8")),
            "characters": len(content),
            "will_change": True,
        }
        if append:
            preview["append"] = True
        elif path.exists():
            preview["overwrites_existing"] = True
            preview["existing_bytes"] = path.stat().st_size
        return preview

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = resolve_workspace_path(context.workspace_dir, arguments["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        content = arguments["content"]
        mode = "a" if arguments.get("append") else "w"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return ToolResult.success(
            f"wrote {len(content)} characters to {arguments['path']}",
            data={"path": arguments["path"], "bytes": len(content.encode("utf-8"))},
        )


class ListDirectoryTool(Tool):
    name = "list_directory"
    description = "List the entries of a workspace directory (non-recursive)."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative directory path (default '.')",
            },
        },
        "additionalProperties": False,
    }
    permissions = (Permission.LIST_DIRECTORY,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path") or "."
        path = resolve_workspace_path(context.workspace_dir, requested, must_exist=True)
        if not path.is_dir():
            raise NotFoundError("path is not a directory", details={"path": requested})
        entries = sorted(
            (
                {
                    "name": child.name,
                    "type": "dir" if child.is_dir() else "file",
                    "size": child.stat().st_size if child.is_file() else None,
                }
                for child in path.iterdir()
            ),
            key=lambda entry: str(entry["name"]),
        )
        return ToolResult.success(
            f"listed {len(entries)} entries in {requested}",
            data={"path": requested, "entries": entries},
        )
