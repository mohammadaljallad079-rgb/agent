"""Generated-artifact discovery shared by every product surface.

A run writes real files into its sandboxed workspace. Both the desktop surface
(inside the native webview) and the browser-served online surface need to list
those files, read one back, and hand one to the operating system. The logic is
identical and security-critical — every path is resolved through the run's
:class:`~arabic_agent.local.sandbox.FilesystemSandbox`, so a ``..`` escape or an
absolute path outside the workspace is refused — so it lives here once rather
than being copied into each surface.

Nothing in this module trusts a caller-supplied path: the workspace comes from
the stored run record, and the requested path is resolved against a
workspace-only policy before any I/O happens.
"""

from __future__ import annotations

import logging
import mimetypes
import subprocess
import sys
from pathlib import Path
from typing import Any

from .core.errors import NotFoundError, PermissionDeniedError, ValidationError
from .local.sandbox import FilesystemPolicy, FilesystemSandbox

logger = logging.getLogger(__name__)

#: Extensions that select a document the runtime can generate. A file with any
#: other extension in a workspace is not reported as an artifact.
ARTIFACT_SUFFIXES: frozenset[str] = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".md", ".markdown", ".txt", ".html", ".json"}
)

#: Artifact formats that can be handed to the operating system's launcher.
OPENABLE_SUFFIXES: frozenset[str] = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".md", ".txt"}
)

#: Artifact formats safe to serve back over HTTP as a download. Everything in
#: :data:`ARTIFACT_SUFFIXES` is data, never executable markup that a browser
#: would run in the origin: HTML and JSON are served as attachments with a
#: generic content type, and everything else keeps its real type.
DOWNLOAD_MEDIA_TYPES: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".markdown": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    # Served as a download with a non-rendering type so a stored page cannot
    # execute script in the API origin.
    ".html": "application/octet-stream",
    ".json": "application/octet-stream",
}


def run_workspace(workspace_dir: str) -> Path | None:
    """Resolve a run's workspace root through the sandbox.

    Returns ``None`` when the record carries no workspace or the path cannot be
    resolved, so a caller reports "no artifacts" instead of raising.
    """
    workspace = (workspace_dir or "").strip()
    if not workspace:
        return None
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(workspace))
    try:
        return sandbox.resolve_for_read(".")
    except (PermissionDeniedError, ValidationError, OSError):
        return None


def list_artifacts(root: Path, *, run_id: str) -> list[dict[str, Any]]:
    """Real generated files under a workspace root, newest first.

    A directory listing filtered to generatable document types — never a
    database of claims. It walks only inside ``root``, so it cannot expose a
    host file.
    """
    artifacts: list[dict[str, Any]] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in ARTIFACT_SUFFIXES:
            continue
        try:
            stat = path.stat()
            relative = path.relative_to(root).as_posix()
        except (OSError, ValueError):  # vanished between glob and stat, or outside root
            continue
        artifacts.append(
            {
                "name": path.name,
                "path": relative,
                "format": suffix.lstrip("."),
                "bytes": stat.st_size,
                "modified_at": stat.st_mtime,
                "openable": suffix in OPENABLE_SUFFIXES,
                "run_id": run_id,
            }
        )
    artifacts.sort(key=lambda a: float(a["modified_at"]), reverse=True)
    return artifacts


def resolve_artifact(workspace_dir: str, relative_path: str) -> Path:
    """Resolve one artifact path inside a run workspace, or raise.

    The path is resolved through a workspace-only sandbox, so only a real file
    inside that workspace can be reached.
    """
    root = run_workspace(workspace_dir)
    if root is None or not root.is_dir():
        raise NotFoundError("run workspace is not available")
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(root)))
    resolved = sandbox.resolve_for_read(relative_path)
    if not resolved.is_file():
        raise NotFoundError("artifact does not exist", details={"path": relative_path})
    return resolved


def media_type_for(path: Path) -> str:
    """The content type to serve ``path`` back with, never a rendering HTML type."""
    suffix = path.suffix.lower()
    known = DOWNLOAD_MEDIA_TYPES.get(suffix)
    if known:
        return known
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


def launch_with_system(path: Path) -> bool:
    """Open ``path`` in the OS default application. Returns whether it launched.

    The desktop UI runs inside a native webview with no network origin, so it
    cannot open a file itself; this is the one place a generated artifact is
    handed to the operating system. A missing launcher is reported as ``False``
    rather than raising, so the UI can say so honestly.
    """
    try:
        if sys.platform.startswith("win"):
            import os

            # ``os.startfile`` exists only on Windows; resolving it via
            # ``getattr`` keeps the behaviour identical without a
            # platform-specific ignore that POSIX mypy would flag as unused.
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                return False
            startfile(str(path))
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(  # noqa: S603 - fixed opener, no shell, sandbox-resolved path
            [opener, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not open artifact in a system application", exc_info=True)
        return False


__all__ = [
    "ARTIFACT_SUFFIXES",
    "DOWNLOAD_MEDIA_TYPES",
    "OPENABLE_SUFFIXES",
    "launch_with_system",
    "list_artifacts",
    "media_type_for",
    "resolve_artifact",
    "run_workspace",
]
