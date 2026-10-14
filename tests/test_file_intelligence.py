"""Phase 6: filesystem intelligence tools and sandbox safety.

These exercise the *real* tools through the real executor/policy path (a fake
model only selects the tool). They cover bounded reads, search, precise edits,
metadata, and the sandbox's refusal of traversal, absolute-path and symlink
escapes.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from arabic_agent.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin._fileops import (
    build_tree,
    classify_file,
    edit_file_exact,
    file_hash,
    read_line_range,
    search_file_contents,
)
from arabic_agent.tools.builtin.filesystem_extra import (
    AppendFileTool,
    CreateFileTool,
    DirectoryTreeTool,
    EditFileTool,
    FileInfoTool,
    ReadFileRangeTool,
    SearchFilesTool,
)

pytestmark = pytest.mark.asyncio


def _ctx(workspace: Path, **metadata: Any) -> ToolContext:
    return ToolContext(
        tenant_id="t1",
        user_id="u1",
        run_id="r1",
        workspace_dir=str(workspace),
        metadata=metadata,
    )


# --- pure ops ---------------------------------------------------------------


async def test_read_line_range_is_bounded_and_reports_more(tmp_path: Path) -> None:
    path = tmp_path / "big.txt"
    path.write_text("\n".join(f"line {i}" for i in range(1, 1001)), encoding="utf-8")
    result = read_line_range(path, start_line=10, end_line=20)
    assert result.start_line == 10
    assert result.end_line == 20
    assert "line 10" in result.text and "line 20" in result.text
    assert "line 21" not in result.text
    assert result.has_more is True  # lines 21.. exist


async def test_read_line_range_byte_cap_sets_has_more(tmp_path: Path) -> None:
    path = tmp_path / "wide.txt"
    path.write_text("x" * 5000 + "\n" + "y" * 5000 + "\n", encoding="utf-8")
    result = read_line_range(path, start_line=1, max_bytes=1024)
    assert result.truncated is True
    assert result.has_more is True


async def test_edit_exact_refuses_ambiguous_and_missing(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("a = 1\nb = 2\na = 1\n", encoding="utf-8")
    with pytest.raises(ConflictError):
        edit_file_exact(path, old="a = 1", new="a = 9")
    with pytest.raises(NotFoundError):
        edit_file_exact(path, old="zzz", new="q")
    result = edit_file_exact(path, old="a = 1", new="a = 9", replace_all=True)
    assert result.replacements == 2
    assert path.read_text(encoding="utf-8") == "a = 9\nb = 2\na = 9\n"


async def test_edit_expected_count_mismatch_fails(tmp_path: Path) -> None:
    path = tmp_path / "f.txt"
    path.write_text("hello world", encoding="utf-8")
    with pytest.raises(ConflictError):
        edit_file_exact(path, old="hello", new="hi", expected_replacements=2)


async def test_edit_preserves_utf8_arabic(tmp_path: Path) -> None:
    path = tmp_path / "ar.txt"
    path.write_text("مرحبا بالعالم", encoding="utf-8")
    edit_file_exact(path, old="بالعالم", new="بالعالم العربي")
    assert path.read_text(encoding="utf-8") == "مرحبا بالعالم العربي"


async def test_classify_and_hash(tmp_path: Path) -> None:
    text = tmp_path / "a.py"
    text.write_text("print(1)", encoding="utf-8")
    binary = tmp_path / "b.bin"
    binary.write_bytes(b"\x00\x01\x02")
    assert classify_file(text) == "text"
    assert classify_file(binary) == "binary"
    assert classify_file(tmp_path) == "directory"
    assert len(file_hash(text)) == 64


async def test_build_tree_is_bounded(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    _node, count, truncated = build_tree(tmp_path, max_entries=10)
    assert truncated is True
    assert count <= 10


async def test_search_contents_finds_lines(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def target():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("nothing\n", encoding="utf-8")
    matches, truncated = search_file_contents(tmp_path, "target")
    assert len(matches) == 1
    assert matches[0].line == 1
    assert truncated is False


# --- tools through the sandbox ---------------------------------------------


async def test_read_file_range_tool(tmp_path: Path) -> None:
    path = tmp_path / "src.txt"
    path.write_text("one\ntwo\nthree\nfour\n", encoding="utf-8")
    result = await ReadFileRangeTool().run(
        {"path": "src.txt", "start_line": 2, "end_line": 3}, _ctx(tmp_path)
    )
    assert result.ok
    assert result.data["content"] == "two\nthree\n"
    assert result.data["has_more"] is True


async def test_read_file_range_tool_normalises_windows_line_endings(tmp_path: Path) -> None:
    # A file written on Windows has CRLF endings; the tool result is text the
    # model consumes, so it must read back identically on every platform.
    path = tmp_path / "win.txt"
    path.write_bytes(b"one\r\ntwo\r\nthree\r\nfour\r\n")
    result = await ReadFileRangeTool().run(
        {"path": "win.txt", "start_line": 2, "end_line": 3}, _ctx(tmp_path)
    )
    assert result.ok
    assert result.data["content"] == "two\nthree\n"
    assert result.data["has_more"] is True


async def test_edit_file_tool(tmp_path: Path) -> None:
    path = tmp_path / "code.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    result = await EditFileTool().run(
        {"path": "code.py", "old": "VALUE = 1", "new": "VALUE = 42"}, _ctx(tmp_path)
    )
    assert result.ok
    assert "VALUE = 42" in path.read_text(encoding="utf-8")


async def test_append_and_create_tools(tmp_path: Path) -> None:
    await CreateFileTool().run({"path": "new.txt", "content": "أ"}, _ctx(tmp_path))
    await AppendFileTool().run({"path": "new.txt", "content": "ب"}, _ctx(tmp_path))
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "أب"


async def test_create_file_refuses_overwrite(tmp_path: Path) -> None:
    (tmp_path / "x.txt").write_text("old", encoding="utf-8")
    with pytest.raises(ConflictError):
        await CreateFileTool().run({"path": "x.txt", "content": "new"}, _ctx(tmp_path))


async def test_directory_tree_tool(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x", encoding="utf-8")
    result = await DirectoryTreeTool().run({"path": ".", "max_depth": 3}, _ctx(tmp_path))
    assert result.ok
    assert result.data["entries"] >= 2


async def test_search_files_tool_by_name_and_content(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("import os\n", encoding="utf-8")
    by_name = await SearchFilesTool().run({"pattern": "*.py"}, _ctx(tmp_path))
    assert any("a.py" in m["path"] for m in by_name.data["name_matches"])
    by_content = await SearchFilesTool().run({"contains": "import os"}, _ctx(tmp_path))
    assert len(by_content.data["content_matches"]) == 2


async def test_file_info_tool(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("hello", encoding="utf-8")
    result = await FileInfoTool().run({"path": "f.txt"}, _ctx(tmp_path))
    assert result.ok
    assert result.data["kind"] == "text"
    assert result.data["sha256"]


# --- sandbox escapes --------------------------------------------------------


async def test_traversal_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionDeniedError):
        await ReadFileRangeTool().run({"path": "../etc/passwd", "start_line": 1}, _ctx(tmp_path))


async def test_absolute_path_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionDeniedError):
        await ReadFileRangeTool().run({"path": "/etc/passwd", "start_line": 1}, _ctx(tmp_path))


async def test_symlink_escape_is_refused(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    link = workspace / "link.txt"
    try:
        os.symlink(outside, link)
    except OSError:
        pytest.skip("symlinks not supported on this platform")
    # The local sandbox refuses symlinks that resolve outside the workspace.
    from arabic_agent.local.sandbox import FilesystemPolicy, FilesystemSandbox

    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(workspace)))
    decision = sandbox.authorize_read(str(link))
    assert decision.allowed is False


async def test_edit_outside_workspace_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PermissionDeniedError):
        await EditFileTool().run({"path": "../../evil.txt", "old": "a", "new": "b"}, _ctx(tmp_path))
