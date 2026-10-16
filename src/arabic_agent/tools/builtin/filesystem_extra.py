"""Extended workspace filesystem tools (Phase 6).

These are the sandboxed, policy-gated tools that let the agent work like an
operator on a project: read a range of a large file, search names and contents,
make precise edits, copy/move/delete/create, inspect metadata and classify file
types. Every one resolves its path through the workspace sandbox
(``_paths.resolve_workspace_path``) and is executed by the existing
:class:`~arabic_agent.agent.executor.ToolExecutor`, so permissions, approval,
idempotency and audit are unchanged.

The heavy lifting lives in :mod:`._fileops` so behaviour is identical to the
local runtime's tools and there is one implementation to test.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, ClassVar

from ...core.errors import NotFoundError, ValidationError
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult
from ._fileops import (
    DEFAULT_MAX_SEARCH_RESULTS,
    SKIP_DIRS,
    append_text,
    build_tree,
    classify_file,
    copy_file,
    create_file,
    delete_file,
    edit_file_exact,
    file_hash,
    move_file,
    read_line_range,
    search_file_contents,
    stat_path,
    tree_to_dict,
)
from ._paths import resolve_workspace_path


class _WorkspaceTool(Tool):
    """Shared path resolution for workspace tools."""

    def _read(self, context: ToolContext, requested: str) -> Path:
        return resolve_workspace_path(context.workspace_dir, requested, must_exist=True)

    def _write(self, context: ToolContext, requested: str) -> Path:
        return resolve_workspace_path(context.workspace_dir, requested)


class ReadFileRangeTool(_WorkspaceTool):
    name = "read_file_range"
    description = (
        "Read a line range from a workspace text file (1-based, inclusive). "
        "Use this for large files: it streams only the requested lines and "
        "reports whether more content follows."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "start_line": {"type": "integer", "description": "First line (1-based)"},
            "end_line": {"type": "integer", "description": "Last line (inclusive)"},
        },
        "required": ["path", "start_line"],
        "additionalProperties": False,
    }
    permissions = (Permission.READ_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._read(context, arguments["path"])
        if not path.is_file():
            raise ValidationError("path is not a regular file", details={"path": arguments["path"]})
        result = read_line_range(
            path,
            start_line=int(arguments["start_line"]),
            end_line=(int(arguments["end_line"]) if arguments.get("end_line") else None),
        )
        return ToolResult.success(
            f"read lines {result.start_line}-{result.end_line} of {arguments['path']}",
            data={
                "path": arguments["path"],
                "content": result.text,
                "start_line": result.start_line,
                "end_line": result.end_line,
                "total_lines": result.total_lines,
                "has_more": result.has_more,
                "truncated": result.truncated,
            },
        )


class EditFileTool(_WorkspaceTool):
    name = "edit_file"
    description = (
        "Edit a workspace text file by exact replacement of a unique block. "
        "Read the file first. Fails loudly if the old text is missing or "
        "ambiguous, so an edit never silently changes the wrong place."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "old": {"type": "string", "description": "Exact text to replace"},
            "new": {"type": "string", "description": "Replacement text"},
            "replace_all": {
                "type": "boolean",
                "description": "Replace every occurrence (default false)",
            },
        },
        "required": ["path", "old", "new"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def preview(
        self, arguments: dict[str, Any], context: ToolContext
    ) -> dict[str, Any] | None:
        path = self._read(context, arguments["path"])
        old = str(arguments.get("old", ""))
        new = str(arguments.get("new", ""))
        preview: dict[str, Any] = {
            "operation": "edit_file",
            "path": arguments["path"],
            "will_change": False,
        }
        if not path.is_file():
            preview["note"] = "path is not a regular file"
            return preview
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            preview["note"] = f"could not read file: {exc}"
            return preview
        occurrences = text.count(old) if old else 0
        preview["occurrences"] = occurrences
        preview["replace_all"] = bool(arguments.get("replace_all"))
        preview["will_change"] = occurrences > 0 and (
            bool(arguments.get("replace_all")) or occurrences == 1
        )
        if occurrences == 0:
            preview["note"] = "'old' text was not found; the edit would fail"
        elif occurrences > 1 and not arguments.get("replace_all"):
            preview["note"] = f"'old' is ambiguous ({occurrences} matches); the edit would fail"
        else:
            preview["old"] = old[:200]
            preview["new"] = new[:200]
        return preview

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._read(context, arguments["path"])
        if not path.is_file():
            raise ValidationError("path is not a regular file", details={"path": arguments["path"]})
        result = edit_file_exact(
            path,
            old=arguments["old"],
            new=arguments["new"],
            replace_all=bool(arguments.get("replace_all")),
        )
        return ToolResult.success(
            f"edited {arguments['path']} ({result.replacements} replacement(s))",
            data={
                "path": arguments["path"],
                "replacements": result.replacements,
                "encoding": result.encoding,
            },
        )


class AppendFileTool(_WorkspaceTool):
    name = "append_file"
    description = "Append UTF-8 text to a workspace file, creating it if needed."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "Text to append"},
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._write(context, arguments["path"])
        written = append_text(path, arguments["content"])
        return ToolResult.success(
            f"appended {written} bytes to {arguments['path']}",
            data={"path": arguments["path"], "bytes": written},
        )


class CreateFileTool(_WorkspaceTool):
    name = "create_file"
    description = "Create a new workspace file. Refuses to overwrite unless told to."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Workspace-relative file path"},
            "content": {"type": "string", "description": "Initial content"},
            "overwrite": {"type": "boolean", "description": "Overwrite if it exists"},
        },
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._write(context, arguments["path"])
        written = create_file(
            path, arguments.get("content", ""), overwrite=bool(arguments.get("overwrite"))
        )
        return ToolResult.success(
            f"created {arguments['path']} ({written} bytes)",
            data={"path": arguments["path"], "bytes": written},
        )


class CopyFileTool(_WorkspaceTool):
    name = "copy_file"
    description = "Copy a file within the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path"},
            "destination": {"type": "string", "description": "Destination path"},
            "overwrite": {"type": "boolean", "description": "Overwrite if it exists"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source = self._read(context, arguments["source"])
        destination = self._write(context, arguments["destination"])
        copy_file(source, destination, overwrite=bool(arguments.get("overwrite")))
        return ToolResult.success(
            f"copied {arguments['source']} to {arguments['destination']}",
            data={"source": arguments["source"], "destination": arguments["destination"]},
        )


class MoveFileTool(_WorkspaceTool):
    name = "move_file"
    description = "Move or rename a file within the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source path"},
            "destination": {"type": "string", "description": "Destination path"},
            "overwrite": {"type": "boolean", "description": "Overwrite if it exists"},
        },
        "required": ["source", "destination"],
        "additionalProperties": False,
    }
    permissions = (Permission.MOVE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        source = self._read(context, arguments["source"])
        destination = self._write(context, arguments["destination"])
        move_file(source, destination, overwrite=bool(arguments.get("overwrite")))
        return ToolResult.success(
            f"moved {arguments['source']} to {arguments['destination']}",
            data={"source": arguments["source"], "destination": arguments["destination"]},
        )


class DeleteFileTool(_WorkspaceTool):
    name = "delete_file"
    description = (
        "Delete a single workspace file. Denied by default by policy; a tenant "
        "must explicitly opt in. Requires approval."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "File path to delete"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.DELETE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._read(context, arguments["path"])
        delete_file(path)
        return ToolResult.success(f"deleted {arguments['path']}", data={"path": arguments["path"]})


class MakeDirectoryTool(_WorkspaceTool):
    name = "make_directory"
    description = "Create a directory (with parents) inside the workspace."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Directory path to create"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.WRITE_FILE,)
    side_effecting: ClassVar[bool] = True

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._write(context, arguments["path"])
        path.mkdir(parents=True, exist_ok=True)
        return ToolResult.success(
            f"created directory {arguments['path']}", data={"path": arguments["path"]}
        )


class FileInfoTool(_WorkspaceTool):
    name = "file_info"
    description = (
        "Return bounded metadata for a workspace path: type, size, kind "
        "(text/binary/image/archive/document), symlink flag and sha256."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Workspace-relative path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.READ_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._read(context, arguments["path"])
        info = stat_path(path)
        if path.is_file():
            info["sha256"] = file_hash(path)
        return ToolResult.success(
            f"{arguments['path']}: {info['kind']}", data={"path": arguments["path"], **info}
        )


class DirectoryTreeTool(_WorkspaceTool):
    name = "directory_tree"
    description = (
        "Return a bounded directory tree of the workspace. Depth and entry "
        "counts are capped; the result reports whether it was truncated."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Root directory (default '.')"},
            "max_depth": {"type": "integer", "description": "Max depth (default 3)"},
            "max_entries": {"type": "integer", "description": "Max entries (default 500)"},
        },
        "additionalProperties": False,
    }
    permissions = (Permission.LIST_DIRECTORY,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        requested = arguments.get("path") or "."
        root = self._read(context, requested)
        if not root.is_dir():
            raise NotFoundError("path is not a directory", details={"path": requested})
        node, count, truncated = build_tree(
            root,
            max_depth=int(arguments.get("max_depth") or 3),
            max_entries=int(arguments.get("max_entries") or 500),
        )
        return ToolResult.success(
            f"tree of {requested}: {count} entries" + (" (truncated)" if truncated else ""),
            data={
                "path": requested,
                "tree": tree_to_dict(node),
                "entries": count,
                "truncated": truncated,
            },
        )


class SearchFilesTool(_WorkspaceTool):
    name = "search_files"
    description = (
        "Recursively search the workspace for files whose name matches a glob "
        "(e.g. '*.py') or whose contents contain a literal substring. Provide "
        "'pattern' for names, 'contains' for content, or both."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Root directory (default '.')"},
            "pattern": {"type": "string", "description": "Glob for file names"},
            "contains": {"type": "string", "description": "Literal text to find in contents"},
        },
        "additionalProperties": False,
    }
    permissions = (Permission.LIST_DIRECTORY,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        import fnmatch

        requested = arguments.get("path") or "."
        root = self._read(context, requested)
        if not root.is_dir():
            raise NotFoundError("path is not a directory", details={"path": requested})
        pattern = arguments.get("pattern")
        contains = arguments.get("contains")
        if not pattern and not contains:
            raise ValidationError("provide 'pattern', 'contains', or both")

        name_matches: list[dict[str, Any]] = []
        if pattern:
            for current_dir, dir_names, file_names in os.walk(root):
                dir_names[:] = [d for d in dir_names if d not in SKIP_DIRS]
                for file_name in file_names:
                    if fnmatch.fnmatch(file_name, pattern):
                        candidate = Path(current_dir) / file_name
                        name_matches.append(
                            {"path": str(candidate), "size": candidate.stat().st_size}
                        )
                        if len(name_matches) >= DEFAULT_MAX_SEARCH_RESULTS:
                            break
                if len(name_matches) >= DEFAULT_MAX_SEARCH_RESULTS:
                    break

        content_matches: list[dict[str, Any]] = []
        content_truncated = False
        if contains:
            found, content_truncated = search_file_contents(root, contains)
            content_matches = [{"path": m.path, "line": m.line, "text": m.text} for m in found]

        return ToolResult.success(
            f"found {len(name_matches)} name match(es), {len(content_matches)} content match(es)",
            data={
                "path": requested,
                "name_matches": name_matches,
                "content_matches": content_matches,
                "content_truncated": content_truncated,
            },
        )


class FileTypeTool(_WorkspaceTool):
    name = "file_type"
    description = "Classify a workspace path: text, binary, image, archive or document."
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "Workspace-relative path"}},
        "required": ["path"],
        "additionalProperties": False,
    }
    permissions = (Permission.READ_FILE,)

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        path = self._read(context, arguments["path"])
        kind = classify_file(path)
        return ToolResult.success(
            f"{arguments['path']} is {kind}", data={"path": arguments["path"], "kind": kind}
        )


#: The extended workspace filesystem tools.
EXTENDED_FILESYSTEM_TOOLS: tuple[type[Tool], ...] = (
    ReadFileRangeTool,
    EditFileTool,
    AppendFileTool,
    CreateFileTool,
    CopyFileTool,
    MoveFileTool,
    DeleteFileTool,
    MakeDirectoryTool,
    FileInfoTool,
    DirectoryTreeTool,
    SearchFilesTool,
    FileTypeTool,
)
