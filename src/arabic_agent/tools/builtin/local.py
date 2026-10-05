"""Sandboxed local filesystem tools.

These are the *real* local capabilities for this phase. Every path is resolved
through the :class:`~arabic_agent.local.sandbox.FilesystemSandbox`, so the
threat model in ``sandbox.py`` (traversal, absolute-path bypass, symlink escape,
sensitive locations, workspace escape) applies to all of them.

The tools are ordinary :class:`~arabic_agent.tools.base.Tool` instances, so they
are executed by the existing :class:`~arabic_agent.agent.executor.ToolExecutor`:
permissions, human approval, idempotency and audit are unchanged. They declare
the *local* ``FILES_*`` permissions, which are distinct from the server-side
file permissions, so a tenant can grant local file access independently.

Only safe foundational capabilities are implemented here. Deleting is
implemented but denied by default by the platform policy (a tenant must opt in);
the high-risk computer-control tools (clipboard, screen, input, printer,
application control) are **not** implemented — they are declared in the
capability catalogue as ``implemented=False``.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Any, ClassVar

from ...core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from ...local.sandbox import FilesystemSandbox
from ...local.sandbox_provider import SandboxProvider
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult

#: Maximum bytes returned to the model in one local read.
MAX_LOCAL_READ_BYTES = 64 * 1024
#: Maximum number of search results returned.
MAX_SEARCH_RESULTS = 200
#: Directories never traversed by the search tool.
_SEARCH_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", ".mypy_cache"})


def sandbox_for(context: ToolContext) -> FilesystemSandbox:
    """Build the sandbox for this call.

    The runtime's :class:`FilesystemPolicy` (operator-approved extra roots and
    deny-list) travels in ``context.metadata["filesystem_policy"]``. The run's
    ``workspace_dir`` is always merged in as an allowed root. With no policy in
    metadata, the sandbox is workspace-only — the safe default.
    """
    return SandboxProvider().for_context(context)


class _LocalFileTool(Tool):
    """Shared base: resolves paths through the sandbox."""

    permissions: tuple[Permission, ...] = ()

    def _resolve_read(self, context: ToolContext, requested: str) -> Path:
        return sandbox_for(context).resolve_for_read(requested)

    def _resolve_write(self, context: ToolContext, requested: str) -> Path:
        return sandbox_for(context).resolve_for_write(requested)


class ReadLocalFileTool(_LocalFileTool):
    name = "read_local_file"
    description = (
        "Read a UTF-8 text file from an allowed local directory. The path is "
        "resolved inside the sandbox; reads outside the allowed roots, "
        "traversal and symlink escapes are refused."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path to read"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._resolve_read(context, arguments["path"])
        if not path.is_file():
            raise NotFoundError("path is not a regular file", details={"path": arguments["path"]})
        raw = path.read_bytes()
        truncated = len(raw) > MAX_LOCAL_READ_BYTES
        chunk = raw[:MAX_LOCAL_READ_BYTES]
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValidationError(
                "file is not valid UTF-8 text", details={"path": arguments["path"]}
            ) from exc
        summary = f"read {len(chunk)} bytes from {arguments['path']}"
        if truncated:
            summary += " (truncated)"
        return ToolResult.success(
            summary,
            data={"path": arguments["path"], "content": text, "truncated": truncated},
        )


class ListLocalDirectoryTool(_LocalFileTool):
    name = "list_local_directory"
    description = "List the entries of an allowed local directory (non-recursive)."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path (default '.')"}},
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_READ,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path") or context.workspace_dir or "."
        path = self._resolve_read(context, requested)
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


class SearchLocalFilesTool(_LocalFileTool):
    name = "search_local_files"
    description = (
        "Recursively search for files whose name matches a glob pattern "
        "(e.g. '*.pdf') inside an allowed local directory."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to search (default '.')"},
            "pattern": {"type": "string", "description": "Glob pattern, e.g. '*.pdf'"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_SEARCH,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path") or context.workspace_dir or "."
        pattern = arguments["pattern"]
        sandbox = sandbox_for(context)
        root = sandbox.resolve_for_read(requested)
        if not root.is_dir():
            raise NotFoundError("path is not a directory", details={"path": requested})

        matches: list[dict[str, Any]] = []
        for current_dir, dir_names, file_names in os.walk(root):
            current = Path(current_dir)
            dir_names[:] = [
                d
                for d in dir_names
                if d not in _SEARCH_SKIP_DIRS and sandbox.authorize_read(str(current / d)).allowed
            ]
            for file_name in file_names:
                if not fnmatch.fnmatch(file_name, pattern):
                    continue
                candidate = current / file_name
                if not sandbox.authorize_read(str(candidate)).allowed:
                    continue
                matches.append(
                    {
                        "path": str(candidate),
                        "size": candidate.stat().st_size if candidate.is_file() else None,
                    }
                )
                if len(matches) >= MAX_SEARCH_RESULTS:
                    break
            if len(matches) >= MAX_SEARCH_RESULTS:
                break
        return ToolResult.success(
            f"found {len(matches)} file(s) matching {pattern}",
            data={"pattern": pattern, "root": str(root), "matches": matches},
        )


class WriteLocalFileTool(_LocalFileTool):
    name = "write_local_file"
    description = (
        "Write UTF-8 text to a file inside an allowed local directory, creating "
        "parent directories as needed. Requires approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "UTF-8 text to write"},
            "append": {"type": "boolean", "description": "Append (default false)"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_WRITE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        sandbox = sandbox_for(context)
        path = sandbox.resolve_for_write(arguments["path"])
        content = arguments["content"]
        encoded = content.encode("utf-8")
        if len(encoded) > sandbox.policy.max_write_bytes:
            raise ValidationError(
                "content exceeds the maximum local write size",
                details={"limit": sandbox.policy.max_write_bytes, "size": len(encoded)},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if arguments.get("append") else "w"
        with path.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        return ToolResult.success(
            f"wrote {len(content)} characters to {arguments['path']}",
            data={"path": arguments["path"], "bytes": len(encoded)},
        )


class CreateLocalDirectoryTool(_LocalFileTool):
    name = "create_local_directory"
    description = "Create a directory inside an allowed local directory. Requires approval."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path to create"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_WRITE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._resolve_write(context, arguments["path"])
        path.mkdir(parents=True, exist_ok=True)
        return ToolResult.success(
            f"created directory {arguments['path']}", data={"path": arguments["path"]}
        )


class CopyLocalFileTool(_LocalFileTool):
    name = "copy_local_file"
    description = "Copy a file within allowed local directories. Requires approval."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source file path"},
            "destination": {"type": "string", "description": "Destination file path"},
            "overwrite": {"type": "boolean", "description": "Overwrite if it exists"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_WRITE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        sandbox = sandbox_for(context)
        source = sandbox.resolve_for_read(arguments["source"])
        destination = sandbox.resolve_for_write(arguments["destination"])
        if not source.is_file():
            raise NotFoundError("source is not a file", details={"path": arguments["source"]})
        if destination.exists() and not arguments.get("overwrite"):
            raise ConflictError(
                "destination already exists; pass overwrite=true to replace it",
                details={"path": arguments["destination"]},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return ToolResult.success(
            f"copied {arguments['source']} to {arguments['destination']}",
            data={"source": arguments["source"], "destination": arguments["destination"]},
        )


class MoveLocalFileTool(_LocalFileTool):
    name = "move_local_file"
    description = "Move or rename a file within allowed local directories. Requires approval."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source file path"},
            "destination": {"type": "string", "description": "Destination file path"},
            "overwrite": {"type": "boolean", "description": "Overwrite if it exists"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_MOVE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        sandbox = sandbox_for(context)
        source = sandbox.resolve_for_read(arguments["source"])
        destination = sandbox.resolve_for_write(arguments["destination"])
        if not source.exists():
            raise NotFoundError("source does not exist", details={"path": arguments["source"]})
        if destination.exists() and not arguments.get("overwrite"):
            raise ConflictError(
                "destination already exists; pass overwrite=true to replace it",
                details={"path": arguments["destination"]},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        return ToolResult.success(
            f"moved {arguments['source']} to {arguments['destination']}",
            data={"source": arguments["source"], "destination": arguments["destination"]},
        )


class DeleteLocalFileTool(_LocalFileTool):
    name = "delete_local_file"
    description = (
        "Delete a file inside an allowed local directory. Denied by default by "
        "policy; a tenant must explicitly opt in. Requires approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path to delete"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.FILES_DELETE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._resolve_write(context, arguments["path"])
        if not path.exists():
            raise NotFoundError("path does not exist", details={"path": arguments["path"]})
        if path.is_dir():
            # Refuse recursive deletion outright; a directory delete is never a
            # single-file operation and is far too destructive to expose here.
            raise ValidationError(
                "refusing to delete a directory; delete files individually",
                details={"path": arguments["path"]},
            )
        path.unlink()
        return ToolResult.success(f"deleted {arguments['path']}", data={"path": arguments["path"]})


class RunLocalCommandTool(_LocalFileTool):
    name = "run_local_command"
    description = (
        "Run an allow-listed local command inside an allowed directory. Commands "
        "are not interpreted by a shell, the environment is minimal, output is "
        "truncated and a timeout applies. Requires approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Command line (argv, no shell)"},
            "cwd": {"type": "string", "description": "Optional working directory"},
            "timeout_seconds": {"type": "number", "description": "Optional timeout"},
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    permissions = (Permission.RUN_COMMAND,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        from ...local.command import CommandPolicy, ControlledCommandRunner

        policy = context.metadata.get("command_policy")
        if not isinstance(policy, CommandPolicy):
            # No allow-list configured → nothing may run. This is the safe
            # default and is explicit rather than silent.
            raise PermissionDeniedError(
                "no local command policy configured; command execution is disabled"
            )
        runner = ControlledCommandRunner(policy)
        result = await runner.run(
            arguments["command"],
            cwd=context.workspace_dir,
            requested_cwd=arguments.get("cwd"),
            timeout_seconds=arguments.get("timeout_seconds"),
        )
        data = {
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "argv": list(result.argv),
            "truncated": result.truncated,
        }
        summary = f"command exited with code {result.exit_code}"
        return (
            ToolResult.success(summary, data=data)
            if result.ok
            else ToolResult.failure(summary, data=data)
        )


#: The local tools implemented in this phase.
LOCAL_TOOLS: tuple[type[Tool], ...] = (
    ReadLocalFileTool,
    ListLocalDirectoryTool,
    SearchLocalFilesTool,
    WriteLocalFileTool,
    CreateLocalDirectoryTool,
    CopyLocalFileTool,
    MoveLocalFileTool,
    DeleteLocalFileTool,
    RunLocalCommandTool,
)
