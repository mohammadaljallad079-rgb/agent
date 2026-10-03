"""Workspace path safety.

Every filesystem tool resolves user-supplied paths against the run workspace
and rejects anything that escapes it. This is the sandbox boundary for the
local runtime; without it a model could read ``/etc/shadow`` or write outside
its tenant's directory.
"""

from __future__ import annotations

from pathlib import Path

from ...core.errors import PermissionDeniedError, ValidationError


def resolve_workspace_path(workspace_dir: str, requested: str, *, must_exist: bool = False) -> Path:
    """Resolve ``requested`` inside ``workspace_dir`` or raise.

    Blocks absolute paths and ``..`` traversal. Returns a fully-resolved path
    guaranteed to live under the workspace root.
    """
    if not workspace_dir or not workspace_dir.strip():
        raise ValidationError("workspace_dir must be provided for filesystem tools")
    if not requested or not requested.strip():
        raise ValidationError("path must not be empty")

    root = Path(workspace_dir).resolve()
    candidate = Path(requested)
    if candidate.is_absolute():
        raise PermissionDeniedError("absolute paths are not permitted", details={"path": requested})
    if ".." in candidate.parts:
        raise PermissionDeniedError(
            "parent-directory traversal is not permitted", details={"path": requested}
        )

    resolved = (root / candidate).resolve()
    if resolved != root and root not in resolved.parents:
        raise PermissionDeniedError(
            "path escapes the workspace sandbox", details={"path": requested}
        )
    if must_exist and not resolved.exists():
        from ...core.errors import NotFoundError

        raise NotFoundError("path does not exist", details={"path": requested})
    return resolved
