"""Tool system tests: schema validation, sandboxing, execution."""

from __future__ import annotations

from pathlib import Path

import pytest

from arabic_agent.core.errors import (
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin import (
    EchoTool,
    ListDirectoryTool,
    ReadFileTool,
    ShellCommandTool,
    WriteFileTool,
)
from arabic_agent.tools.registry import ToolRegistry


def _context(tmp_path) -> ToolContext:
    return ToolContext(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        workspace_dir=str(tmp_path),
    )


def test_registry_rejects_duplicate_registration():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(Exception):
        registry.register(EchoTool())


def test_registry_unknown_tool_raises_not_found():
    registry = ToolRegistry([EchoTool()])
    with pytest.raises(NotFoundError):
        registry.get("nope")


async def test_echo_returns_input(tmp_path):
    result = await EchoTool().execute({"text": "مرحبا"}, _context(tmp_path))
    assert result.ok
    assert result.data["text"] == "مرحبا"


async def test_missing_required_argument_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        await EchoTool().execute({}, _context(tmp_path))


async def test_unknown_argument_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        await EchoTool().execute({"text": "x", "extra": 1}, _context(tmp_path))


async def test_write_then_read_roundtrip(tmp_path):
    context = _context(tmp_path)
    write = await WriteFileTool().execute({"path": "notes/todo.txt", "content": "سطر أول"}, context)
    assert write.ok
    read = await ReadFileTool().execute({"path": "notes/todo.txt"}, context)
    assert read.ok
    assert read.data["content"] == "سطر أول"


async def test_read_missing_file_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await ReadFileTool().execute({"path": "missing.txt"}, _context(tmp_path))


async def test_path_traversal_is_blocked(tmp_path):
    with pytest.raises(PermissionDeniedError):
        await ReadFileTool().execute({"path": "../../etc/passwd"}, _context(tmp_path))


async def test_absolute_path_is_blocked(tmp_path):
    with pytest.raises(PermissionDeniedError):
        await WriteFileTool().execute({"path": "/tmp/evil", "content": "x"}, _context(tmp_path))


async def test_missing_workspace_is_rejected():
    """An empty workspace must never resolve to the process working directory."""
    context = ToolContext(tenant_id="tnt_1", user_id="usr_1", run_id="run_1", workspace_dir="")
    with pytest.raises(ValidationError):
        await WriteFileTool().execute({"path": "escape.txt", "content": "x"}, context)
    assert not _exists("escape.txt")


async def test_list_directory_reports_entries(tmp_path):
    context = _context(tmp_path)
    await WriteFileTool().execute({"path": "a.txt", "content": "1"}, context)
    result = await ListDirectoryTool().execute({}, context)
    assert result.ok
    names = {entry["name"] for entry in result.data["entries"]}
    assert "a.txt" in names


async def test_shell_command_runs_and_captures_output(tmp_path):
    result = await ShellCommandTool().execute({"command": "echo hello"}, _context(tmp_path))
    assert result.ok
    assert result.data["exit_code"] == 0
    assert "hello" in result.data["stdout"]


async def test_shell_command_nonzero_exit_is_failure(tmp_path):
    result = await ShellCommandTool().execute({"command": "false"}, _context(tmp_path))
    assert not result.ok
    assert result.data["exit_code"] != 0


async def test_shell_command_does_not_leak_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("AAP_SECRET_TOKEN", "super-secret-value")
    result = await ShellCommandTool().execute(
        {"command": "printenv AAP_SECRET_TOKEN"}, _context(tmp_path)
    )
    assert "super-secret-value" not in result.data["stdout"]


async def test_shell_timeout_is_enforced(tmp_path):
    with pytest.raises(Exception):
        await ShellCommandTool().execute(
            {"command": "sleep 5", "timeout_seconds": 0.2}, _context(tmp_path)
        )


def _exists(path: str) -> bool:
    return Path(path).exists()
