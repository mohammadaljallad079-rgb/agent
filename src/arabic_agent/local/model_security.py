"""Local model security.

Model files and their configuration are **untrusted input**. This module holds
the small set of checks that keep a local model from becoming a way to read or
execute arbitrary things on the machine:

* a model path can never escape the approved storage directory (traversal,
  absolute paths and symlink escapes are refused);
* a weights file is verified against its expected hash when one is configured;
* model configuration is parsed as *data* only — it is never ``eval``-ed, never
  imported, and never used to build a command;
* configuration is scanned for embedded credentials so a descriptor cannot
  smuggle a secret into the platform.

None of these functions perform network access or execute a model.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from ..core.errors import ValidationError
from ..core.redaction import _SENSITIVE_KEY_RE  # reuse the single secret-key definition

#: Names that must never appear as configuration keys in a model descriptor.
_FORBIDDEN_CONFIG_KEYS = frozenset({"command", "exec", "shell", "postinstall", "script"})


def resolve_storage_dir(storage_dir: str) -> Path:
    """Resolve and create the approved model storage directory."""
    root = Path(storage_dir).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def ensure_within_storage(path: Path, *, storage_dir: Path) -> Path:
    """Confine a model path to the approved storage directory.

    Rejects traversal, absolute paths outside the store, and symlink escapes so
    a descriptor can never point the loader at an arbitrary file elsewhere on
    the machine.
    """
    root = storage_dir.expanduser().resolve()
    resolved = path.expanduser().resolve()
    if resolved != root and root not in resolved.parents:
        raise ValidationError(
            f"model path escapes the approved storage directory: {path}",
            details={"storage_dir": str(root)},
        )
    if path.is_symlink():
        raise ValidationError(f"model path must not be a symlink: {path}")
    return resolved


def verify_model_file(path: Path, *, sha256: str | None) -> None:
    """Check that a weights file exists and, when a hash is given, matches.

    Read in chunks so a multi-gigabyte model does not have to fit in memory. A
    mismatch is a hard failure — a corrupted or tampered file must not load.
    """
    if not path.is_file():
        raise ValidationError(f"model file not found: {path}")
    if not sha256:
        return
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest().lower() != sha256.lower():
        raise ValidationError(f"model file failed integrity check: {path}")


def safe_load_config(text: str) -> dict[str, Any]:
    """Parse a JSON model-config document as data, rejecting unsafe content.

    The document is never executed. Keys that name a command or script are
    refused, as are keys that look like credentials, so a config file cannot
    turn into a code-execution or secret-exfiltration vector.
    """
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(f"model config is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValidationError("model config must be a JSON object")
    _reject_unsafe_keys(payload)
    return payload


def _reject_unsafe_keys(payload: dict[str, Any]) -> None:
    for key, value in payload.items():
        lowered = str(key).lower()
        if lowered in _FORBIDDEN_CONFIG_KEYS:
            raise ValidationError(f"model config may not contain a {key!r} key")
        if _SENSITIVE_KEY_RE.search(lowered):
            raise ValidationError(
                f"model config may not embed credentials (key {key!r}); "
                "inject secrets via the environment instead"
            )
        if isinstance(value, dict):
            _reject_unsafe_keys(value)


def path_is_within(path: Path, *, root: Path) -> bool:
    """Return whether ``path`` is inside ``root`` (no symlink following)."""
    resolved_root = os.path.realpath(root)
    resolved_path = os.path.realpath(path)
    return resolved_path == resolved_root or resolved_path.startswith(resolved_root + os.sep)
