"""Pure filesystem operations shared by the workspace and local tool suites.

These helpers do no path authorisation — the caller resolves a path through its
own sandbox (``tools/builtin/_paths.py`` for the workspace suite, the
:class:`~arabic_agent.local.sandbox.FilesystemSandbox` for the local suite) and
passes the *already resolved* :class:`~pathlib.Path` here. Keeping the I/O
logic in one place means both suites behave identically and there is a single
implementation to reason about, test and audit.

All operations are bounded: reads are capped in bytes, searches in results,
tree walks in depth and entry count, and edits in replacement count. Nothing
here silently truncates without reporting that it did.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ...core.errors import ConflictError, NotFoundError, ValidationError

#: Default cap on the bytes returned by a range read.
DEFAULT_MAX_READ_BYTES = 64 * 1024
#: Default cap on lines returned by a range read.
DEFAULT_MAX_READ_LINES = 2000
#: Default cap on results returned by a content search.
DEFAULT_MAX_SEARCH_RESULTS = 200
#: Default cap on files inspected by a content search.
DEFAULT_MAX_SEARCH_FILES = 5000
#: Default maximum size of a file that will be content-searched (bytes).
DEFAULT_MAX_SEARCH_FILE_BYTES = 2 * 1024 * 1024
#: Directories skipped by tree/search walks (noise and VCS metadata).
SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
        "build",
        ".next",
        ".turbo",
    }
)

#: Extensions classified as text by :func:`classify_file`.
_TEXT_SUFFIXES = frozenset(
    {
        ".txt",
        ".md",
        ".markdown",
        ".rst",
        ".json",
        ".jsonl",
        ".csv",
        ".tsv",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".log",
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".hpp",
        ".cs",
        ".go",
        ".rs",
        ".rb",
        ".php",
        ".dart",
        ".swift",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".html",
        ".htm",
        ".css",
        ".scss",
        ".env",
    }
)
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff", ".svg"})
_ARCHIVE_SUFFIXES = frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar"})
_DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt", ".odt", ".ods", ".odp"}
)
_BINARY_HINTS = b"\x00"


@dataclass(frozen=True)
class ReadResult:
    """Outcome of a bounded text read."""

    text: str
    start_line: int
    end_line: int
    total_lines: int
    truncated: bool
    has_more: bool
    encoding: str = "utf-8"


@dataclass(frozen=True)
class EditResult:
    """Outcome of an exact-replacement edit."""

    replacements: int
    bytes_written: int
    encoding: str


@dataclass(frozen=True)
class SearchMatch:
    """One content-search hit with its provenance."""

    path: str
    line: int
    text: str


@dataclass(frozen=True)
class TreeNode:
    """One node in a bounded directory tree."""

    name: str
    path: str
    type: str  # "file" | "dir"
    size: int | None = None
    children: list[TreeNode] = field(default_factory=list)


def detect_encoding(raw: bytes) -> str:
    """Best-effort encoding detection without an extra dependency.

    Handles a UTF-8 BOM, then UTF-8, then falls back to latin-1 so a read never
    crashes on a stray byte (the caller is told which encoding was used).
    """
    if raw.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return "latin-1"
    return "utf-8"


def read_text_file(path: Path, *, max_bytes: int = DEFAULT_MAX_READ_BYTES) -> ReadResult:
    """Read a whole text file, bounded, reporting truncation honestly."""
    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    truncated = len(raw) > max_bytes
    text = raw[:max_bytes].decode(encoding, errors="replace")
    total_lines = text.count("\n") + (0 if text.endswith("\n") or not text else 1)
    return ReadResult(
        text=text,
        start_line=1,
        end_line=total_lines,
        total_lines=total_lines,
        truncated=truncated,
        has_more=truncated,
        encoding=encoding,
    )


def read_line_range(
    path: Path,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_bytes: int = DEFAULT_MAX_READ_BYTES,
    max_lines: int = DEFAULT_MAX_READ_LINES,
) -> ReadResult:
    """Read lines ``[start_line, end_line]`` (1-based, inclusive), bounded.

    Never loads the whole file into memory: it streams line by line, stopping at
    the byte/line cap and reporting ``has_more`` so the agent knows to continue.
    """
    if start_line < 1:
        raise ValidationError("start_line must be >= 1")
    if end_line is not None and end_line < start_line:
        raise ValidationError("end_line must be >= start_line")

    with path.open("rb") as probe:
        encoding = detect_encoding(probe.read(4))

    collected: list[str] = []
    bytes_used = 0
    line_no = 0
    truncated = False
    has_more = False
    last_line = start_line - 1
    with path.open("r", encoding=encoding, errors="replace", newline="") as handle:
        for line_no, line in enumerate(handle, start=1):
            if line_no < start_line:
                continue
            if end_line is not None and line_no > end_line:
                has_more = True
                break
            encoded_len = len(line.encode(encoding, errors="replace"))
            if collected and (bytes_used + encoded_len > max_bytes or len(collected) >= max_lines):
                truncated = True
                has_more = True
                break
            if encoded_len > max_bytes and not collected:
                collected.append(line[:max_bytes])
                bytes_used = max_bytes
                truncated = True
                has_more = True
                last_line = line_no
                break
            collected.append(line)
            bytes_used += encoded_len
            last_line = line_no

    return ReadResult(
        text="".join(collected),
        start_line=start_line,
        end_line=last_line,
        total_lines=line_no,
        truncated=truncated,
        has_more=has_more,
        encoding=encoding,
    )


def edit_file_exact(
    path: Path,
    *,
    old: str,
    new: str,
    replace_all: bool = False,
    expected_replacements: int | None = None,
) -> EditResult:
    """Replace exact text in a file, preserving encoding and line endings.

    The file is read first; the replacement count is checked against
    ``expected_replacements`` when supplied so an edit that matches the wrong
    number of places fails loudly instead of corrupting the file.
    """
    if not old:
        raise ValidationError("old text must not be empty")
    raw = path.read_bytes()
    encoding = detect_encoding(raw)
    original = raw.decode(encoding, errors="replace")
    occurrences = original.count(old)
    if occurrences == 0:
        raise NotFoundError("old text was not found in the file", details={"path": str(path)})
    if expected_replacements is not None and occurrences != expected_replacements:
        raise ConflictError(
            "replacement count did not match expectation",
            details={"found": occurrences, "expected": expected_replacements},
        )
    if occurrences > 1 and not replace_all:
        raise ConflictError(
            "old text appears more than once; pass replace_all=true or a larger unique block",
            details={"found": occurrences},
        )
    updated = original.replace(old, new) if replace_all else original.replace(old, new, 1)
    replacements = occurrences if replace_all else 1
    path.write_bytes(updated.encode(encoding))
    return EditResult(
        replacements=replacements,
        bytes_written=len(updated.encode(encoding)),
        encoding=encoding,
    )


def append_text(path: Path, text: str) -> int:
    """Append text, creating the file if needed. Returns bytes written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
    return len(text.encode("utf-8"))


def create_file(path: Path, text: str, *, overwrite: bool = False) -> int:
    """Create a new file. Refuses to clobber an existing one unless told to."""
    if path.exists() and not overwrite:
        raise ConflictError(
            "file already exists; pass overwrite=true to replace it",
            details={"path": str(path)},
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def copy_file(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    """Copy a file, refusing to overwrite unless told to."""
    if not source.is_file():
        raise NotFoundError("source is not a file", details={"path": str(source)})
    if destination.exists() and not overwrite:
        raise ConflictError(
            "destination already exists; pass overwrite=true to replace it",
            details={"path": str(destination)},
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def move_file(source: Path, destination: Path, *, overwrite: bool = False) -> None:
    """Move/rename a path, refusing to overwrite unless told to."""
    if not source.exists():
        raise NotFoundError("source does not exist", details={"path": str(source)})
    if destination.exists() and not overwrite:
        raise ConflictError(
            "destination already exists; pass overwrite=true to replace it",
            details={"path": str(destination)},
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(destination))


def delete_file(path: Path) -> None:
    """Delete a single file. Refuses directories (never recursive)."""
    if not path.exists():
        raise NotFoundError("path does not exist", details={"path": str(path)})
    if path.is_dir():
        raise ValidationError(
            "refusing to delete a directory; delete files individually",
            details={"path": str(path)},
        )
    path.unlink()


def file_hash(path: Path, *, algorithm: str = "sha256") -> str:
    """Compute a file hash by streaming (never loads the file into memory)."""
    if algorithm not in hashlib.algorithms_available:
        raise ValidationError("unsupported hash algorithm", details={"algorithm": algorithm})
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_file(path: Path) -> str:
    """Classify a path as text/binary/image/archive/document/other.

    Uses the extension first, then a NUL-byte sniff of the first block so a
    mislabelled or extension-less file is classified by content, not guessed.
    """
    if path.is_dir():
        return "directory"
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return "image"
    if suffix in _ARCHIVE_SUFFIXES:
        return "archive"
    if suffix in _DOCUMENT_SUFFIXES:
        return "document"
    if suffix in _TEXT_SUFFIXES:
        return "text"
    try:
        head = path.open("rb").read(4096)
    except OSError:
        return "other"
    if _BINARY_HINTS in head:
        return "binary"
    return "text"


def stat_path(path: Path) -> dict[str, object]:
    """Return bounded, non-secret metadata for a path."""
    is_link = path.is_symlink()
    info = path.lstat() if is_link else path.stat()
    return {
        "name": path.name,
        "path": str(path),
        "type": "dir" if path.is_dir() else "file",
        "is_symlink": is_link,
        "size": info.st_size,
        "modified": info.st_mtime,
        "kind": classify_file(path) if not path.is_dir() else "directory",
    }


def build_tree(
    root: Path,
    *,
    max_depth: int = 3,
    max_entries: int = 500,
    skip_dirs: frozenset[str] = SKIP_DIRS,
) -> tuple[TreeNode, int, bool]:
    """Build a bounded directory tree.

    Returns ``(root_node, entry_count, truncated)``. ``truncated`` is True when
    the entry cap was hit, so the agent knows the listing is partial.
    """
    counter = {"count": 0}
    truncated = {"value": False}

    def walk(path: Path, depth: int) -> TreeNode:
        node = TreeNode(
            name=path.name or str(path),
            path=str(path),
            type="dir" if path.is_dir() else "file",
            size=path.stat().st_size if path.is_file() else None,
        )
        if not path.is_dir() or depth >= max_depth:
            return node
        try:
            children = sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
        except OSError:
            return node
        for child in children:
            if counter["count"] >= max_entries:
                truncated["value"] = True
                break
            if child.is_dir() and child.name in skip_dirs:
                continue
            counter["count"] += 1
            node.children.append(walk(child, depth + 1))
        return node

    root_node = walk(root, 0)
    return root_node, counter["count"], truncated["value"]


def tree_to_dict(node: TreeNode) -> dict[str, object]:
    """Serialise a tree node to a plain dict for the model."""
    payload: dict[str, object] = {"name": node.name, "type": node.type}
    if node.size is not None:
        payload["size"] = node.size
    if node.children:
        payload["children"] = [tree_to_dict(child) for child in node.children]
    return payload


def search_file_contents(
    root: Path,
    pattern: str,
    *,
    max_results: int = DEFAULT_MAX_SEARCH_RESULTS,
    max_files: int = DEFAULT_MAX_SEARCH_FILES,
    max_file_bytes: int = DEFAULT_MAX_SEARCH_FILE_BYTES,
    skip_dirs: frozenset[str] = SKIP_DIRS,
    case_sensitive: bool = False,
) -> tuple[list[SearchMatch], bool]:
    """Search file *contents* for a literal substring, bounded.

    Returns ``(matches, truncated)``. Binary and oversized files are skipped, so
    a stray archive cannot blow the budget. Only a literal substring is matched
    (no regex) — the model can reason over the hits.
    """
    if not pattern:
        raise ValidationError("pattern must not be empty")
    needle = pattern if case_sensitive else pattern.casefold()
    matches: list[SearchMatch] = []
    truncated = False
    files_seen = 0
    for current_dir, dir_names, file_names in os.walk(root):
        dir_names[:] = [d for d in dir_names if d not in skip_dirs]
        for file_name in sorted(file_names):
            if files_seen >= max_files or len(matches) >= max_results:
                truncated = True
                break
            candidate = Path(current_dir) / file_name
            try:
                if candidate.stat().st_size > max_file_bytes:
                    continue
            except OSError:
                continue
            files_seen += 1
            if classify_file(candidate) != "text":
                continue
            try:
                with candidate.open("r", encoding="utf-8", errors="ignore") as handle:
                    for line_no, line in enumerate(handle, start=1):
                        haystack = line if case_sensitive else line.casefold()
                        if needle in haystack:
                            matches.append(
                                SearchMatch(
                                    path=str(candidate),
                                    line=line_no,
                                    text=line.rstrip("\n")[:500],
                                )
                            )
                            if len(matches) >= max_results:
                                truncated = True
                                break
            except OSError:
                continue
            if truncated:
                break
        if truncated:
            break
    return matches, truncated


__all__ = [
    "DEFAULT_MAX_READ_BYTES",
    "DEFAULT_MAX_READ_LINES",
    "DEFAULT_MAX_SEARCH_RESULTS",
    "SKIP_DIRS",
    "EditResult",
    "ReadResult",
    "SearchMatch",
    "TreeNode",
    "append_text",
    "build_tree",
    "classify_file",
    "copy_file",
    "create_file",
    "delete_file",
    "detect_encoding",
    "edit_file_exact",
    "file_hash",
    "move_file",
    "read_line_range",
    "read_text_file",
    "search_file_contents",
    "stat_path",
    "tree_to_dict",
]
