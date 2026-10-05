"""Local model installation.

Installing a model means writing a large file to disk, so it is deliberately
**explicit**: nothing here runs on its own. The platform never downloads a model
unless a caller asks for it by name, and the source is always a configured,
operator-approved URL — there is no "discover and fetch" behaviour and no hidden
network access.

The installer is honest about what it can and cannot guarantee:

* it downloads to a temporary file and **resumes** if a partial file is present
  (HTTP ``Range``);
* it verifies size and, when the manifest provides one, SHA-256 integrity before
  the file is promoted into place — a failed check leaves no partial model;
* it is cancellable between chunks;
* it stores every model inside the approved storage directory only, and records
  a versioned manifest of what was installed.

A model that cannot be fetched or verified is never registered.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from ..core.errors import ConfigurationError, ValidationError
from .model_security import ensure_within_storage, resolve_storage_dir, verify_model_file

logger = logging.getLogger(__name__)

#: Default chunk size for streaming downloads (1 MiB).
_CHUNK = 1024 * 1024


def _manifest_path(storage_dir: Path) -> Path:
    return storage_dir / "installed_models.json"


def read_manifest(storage_dir: Path) -> dict[str, dict[str, object]]:
    """Read the installed-model manifest; returns ``{}`` when absent."""
    path = _manifest_path(storage_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("ignoring unreadable model manifest: %s", path)
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_manifest(storage_dir: Path, manifest: dict[str, dict[str, object]]) -> None:
    path = _manifest_path(storage_dir)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


class InstallResult:
    """Outcome of an installation attempt."""

    __slots__ = ("detail", "path", "sha256", "size_bytes", "success")

    def __init__(
        self,
        *,
        success: bool,
        path: str | None = None,
        size_bytes: int = 0,
        sha256: str | None = None,
        detail: str = "",
    ) -> None:
        self.success = success
        self.path = path
        self.size_bytes = size_bytes
        self.sha256 = sha256
        self.detail = detail

    def as_dict(self) -> dict[str, object]:
        return {
            "success": self.success,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "detail": self.detail,
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def install_model(
    *,
    model_id: str,
    url: str,
    storage_dir: str,
    filename: str | None = None,
    expected_sha256: str | None = None,
    expected_size: int | None = None,
    timeout_seconds: float = 60.0,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> InstallResult:
    """Download and verify a model into the approved storage directory.

    The download is explicit (this function is only called when a caller asks),
    resumable, integrity-checked and cancellable. The promoted file is confined
    to ``storage_dir``; a partial file is kept as ``.part`` so a later call can
    resume, and it is removed if verification fails.
    """
    root = resolve_storage_dir(storage_dir)
    name = filename or Path(url.split("?")[0]).name or f"{model_id}.gguf"
    # Never let a caller-supplied filename escape the store.
    target = ensure_within_storage(root / name, storage_dir=root)
    part = target.with_suffix(target.suffix + ".part")

    headers: dict[str, str] = {}
    resume_from = 0
    if part.is_file():
        resume_from = part.stat().st_size
        headers["Range"] = f"bytes={resume_from}-"

    mode = "ab" if resume_from else "wb"
    try:
        async with (
            httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True) as client,
            client.stream("GET", url, headers=headers) as response,
        ):
            if response.status_code not in (200, 206):
                return InstallResult(
                    success=False,
                    detail=f"download failed: HTTP {response.status_code}",
                )
            total_header = response.headers.get("Content-Length")
            total = (
                int(total_header) + resume_from
                if total_header is not None and total_header.isdigit()
                else expected_size
            )
            written = resume_from
            with part.open(mode) as handle:
                async for chunk in response.aiter_bytes(_CHUNK):
                    if should_cancel is not None and should_cancel():
                        handle.flush()
                        return InstallResult(
                            success=False,
                            path=str(part),
                            size_bytes=written,
                            detail="cancelled; partial file kept for resume",
                        )
                    handle.write(chunk)
                    written += len(chunk)
                    if on_progress is not None:
                        on_progress(written, total)
    except httpx.HTTPError as exc:
        return InstallResult(
            success=False, path=str(part), detail=f"download transport error: {exc}"
        )

    if expected_size is not None and written != expected_size:
        return InstallResult(
            success=False,
            path=str(part),
            size_bytes=written,
            detail=f"size mismatch: expected {expected_size}, got {written}",
        )
    if expected_sha256 is not None:
        actual = _sha256_file(part)
        if actual.lower() != expected_sha256.lower():
            part.unlink(missing_ok=True)
            return InstallResult(
                success=False,
                detail="integrity check failed; partial file removed",
            )

    part.replace(target)
    digest = expected_sha256 or _sha256_file(target)
    manifest = read_manifest(root)
    manifest[model_id] = {
        "path": target.name,
        "size_bytes": target.stat().st_size,
        "sha256": digest,
        "installed_at": int(time.time()),
        "source": url,
    }
    _write_manifest(root, manifest)
    logger.info("installed local model %s (%d bytes)", model_id, target.stat().st_size)
    return InstallResult(
        success=True,
        path=str(target),
        size_bytes=target.stat().st_size,
        sha256=digest,
        detail="installed and verified",
    )


def verify_installed(model_id: str, *, storage_dir: str) -> InstallResult:
    """Re-verify an installed model against the manifest (no network)."""
    root = resolve_storage_dir(storage_dir)
    manifest = read_manifest(root)
    record = manifest.get(model_id)
    if not record:
        return InstallResult(success=False, detail=f"model {model_id!r} is not installed")
    raw_path = record.get("path")
    if not isinstance(raw_path, str):
        return InstallResult(success=False, detail="manifest entry is malformed")
    path = ensure_within_storage(root / raw_path, storage_dir=root)
    raw_sha = record.get("sha256")
    expected_sha = raw_sha if isinstance(raw_sha, str) else None
    try:
        verify_model_file(path, sha256=expected_sha)
    except ValidationError as exc:
        return InstallResult(success=False, path=str(path), detail=exc.message)
    return InstallResult(
        success=True,
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=expected_sha,
        detail="verified against manifest",
    )


def require_manifest_source(
    manifest: dict[str, dict[str, object]], model_id: str
) -> dict[str, object]:
    """Return the manifest record for a model or raise (used by callers)."""
    record = manifest.get(model_id)
    if not record:
        raise ConfigurationError(f"no manifest entry for model {model_id!r}")
    return record
