"""Document loading: format dispatch, secret scanning and archive safety.

The loader is the one place a file becomes a :class:`Document`. It:

* dispatches to the right parser by format (and refuses unknown formats rather
  than guessing);
* scans the extracted text for likely secrets so the caller can warn and the
  data-boundary policy can refuse to send it to a remote model;
* inspects archives safely (zip-slip, traversal, symlink entries, decompression
  bombs) without ever extracting or executing their contents.

Nothing here performs path authorisation — callers resolve paths through the
workspace sandbox or the local :class:`FilesystemSandbox` first.
"""

from __future__ import annotations

import re
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from ..core.errors import ValidationError
from .models import Document
from .parsers import PARSERS, ParseLimits, format_for, supported_formats

#: Patterns for likely credentials in document text. Deliberately conservative:
#: we flag and warn rather than silently altering content.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{16,}")),
    (
        "password_assignment",
        re.compile(r"(?i)\b(password|passwd|pwd|secret|api[_-]?key)\s*[:=]\s*['\"]?[^\s'\"]{8,}"),
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    """A likely secret and where it was found (never the secret itself)."""

    kind: str
    locator: str


def scan_for_secrets(document: Document) -> tuple[SecretFinding, ...]:
    """Scan a document for likely secrets, returning kinds and locators only."""
    findings: list[SecretFinding] = []
    for section in document.sections:
        for kind, pattern in _SECRET_PATTERNS:
            if pattern.search(section.text):
                findings.append(SecretFinding(kind=kind, locator=section.provenance.locator))
    return tuple(findings)


def scan_text_for_secrets(text: str, *, locator: str) -> tuple[SecretFinding, ...]:
    findings: list[SecretFinding] = []
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            findings.append(SecretFinding(kind=kind, locator=locator))
    return tuple(findings)


def load_document(
    path: Path,
    *,
    display_path: str,
    limits: ParseLimits | None = None,
) -> Document:
    """Parse ``path`` into a :class:`Document` by format.

    Raises :class:`ValidationError` for an unknown format or a format whose
    parser dependency is not installed — never a fabricated empty document.
    """
    limits = limits or ParseLimits()
    fmt = format_for(path)
    if fmt == "unknown":
        raise ValidationError(
            "unsupported document format",
            details={"path": display_path, "supported": list(supported_formats())},
        )
    if fmt not in supported_formats():
        raise ValidationError(
            f"format {fmt!r} requires an optional parser that is not installed",
            details={"path": display_path, "format": fmt, "supported": list(supported_formats())},
        )
    parser = PARSERS[fmt]
    document = parser(path, display_path=display_path, limits=limits)
    return document


# --- Archive safety --------------------------------------------------------

#: Refuse an archive whose declared uncompressed size exceeds this (bomb guard).
DEFAULT_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
#: Refuse an archive with more entries than this.
DEFAULT_MAX_ARCHIVE_ENTRIES = 5000
#: Refuse an entry whose compression ratio exceeds this (zip-bomb guard).
DEFAULT_MAX_COMPRESSION_RATIO = 1000


@dataclass(frozen=True)
class ArchiveEntry:
    """One inspected archive member (metadata only; contents are never run)."""

    name: str
    size: int
    is_dir: bool
    is_symlink: bool
    unsafe: bool
    reason: str = ""


@dataclass(frozen=True)
class ArchiveInspection:
    """The safe inspection result for an archive."""

    path: str
    format: str
    entries: tuple[ArchiveEntry, ...]
    total_size: int
    truncated: bool
    unsafe_entries: tuple[str, ...]
    warnings: tuple[str, ...]


def _entry_is_unsafe(name: str) -> tuple[bool, str]:
    """Whether an archive member name is unsafe to extract."""
    normalised = name.replace("\\", "/")
    if normalised.startswith("/") or re.match(r"^[A-Za-z]:", normalised):
        return True, "absolute path"
    parts = normalised.split("/")
    if ".." in parts:
        return True, "parent-directory traversal"
    return False, ""


def inspect_zip(path: Path, *, display_path: str) -> ArchiveInspection:
    entries: list[ArchiveEntry] = []
    warnings: list[str] = []
    unsafe: list[str] = []
    total = 0
    truncated = False
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        for info in infos:
            if len(entries) >= DEFAULT_MAX_ARCHIVE_ENTRIES:
                truncated = True
                warnings.append("entry cap reached; listing is partial")
                break
            is_dir = info.is_dir()
            is_symlink = (info.external_attr >> 16) & 0o170000 == 0o120000
            bad, reason = _entry_is_unsafe(info.filename)
            if is_symlink:
                bad, reason = True, "symlink entry"
            ratio = (info.compress_size and info.file_size / max(info.compress_size, 1)) or 1
            if ratio > DEFAULT_MAX_COMPRESSION_RATIO:
                bad, reason = True, f"suspicious compression ratio {ratio:.0f}x"
            total += info.file_size
            if bad:
                unsafe.append(info.filename)
            entries.append(
                ArchiveEntry(
                    name=info.filename,
                    size=info.file_size,
                    is_dir=is_dir,
                    is_symlink=is_symlink,
                    unsafe=bad,
                    reason=reason,
                )
            )
    if total > DEFAULT_MAX_ARCHIVE_BYTES:
        warnings.append(
            f"declared uncompressed size {total} exceeds the safe limit "
            f"{DEFAULT_MAX_ARCHIVE_BYTES}; do not extract blindly"
        )
    return ArchiveInspection(
        path=display_path,
        format="zip",
        entries=tuple(entries),
        total_size=total,
        truncated=truncated,
        unsafe_entries=tuple(unsafe),
        warnings=tuple(warnings),
    )


def inspect_tar(path: Path, *, display_path: str) -> ArchiveInspection:
    entries: list[ArchiveEntry] = []
    warnings: list[str] = []
    unsafe: list[str] = []
    total = 0
    truncated = False
    with tarfile.open(path) as archive:
        for member in archive.getmembers():
            if len(entries) >= DEFAULT_MAX_ARCHIVE_ENTRIES:
                truncated = True
                warnings.append("entry cap reached; listing is partial")
                break
            is_symlink = member.issym() or member.islnk()
            bad, reason = _entry_is_unsafe(member.name)
            if is_symlink:
                bad, reason = True, "symlink/hardlink entry"
            total += member.size
            if bad:
                unsafe.append(member.name)
            entries.append(
                ArchiveEntry(
                    name=member.name,
                    size=member.size,
                    is_dir=member.isdir(),
                    is_symlink=is_symlink,
                    unsafe=bad,
                    reason=reason,
                )
            )
    if total > DEFAULT_MAX_ARCHIVE_BYTES:
        warnings.append(
            f"declared uncompressed size {total} exceeds the safe limit "
            f"{DEFAULT_MAX_ARCHIVE_BYTES}; do not extract blindly"
        )
    return ArchiveInspection(
        path=display_path,
        format="tar",
        entries=tuple(entries),
        total_size=total,
        truncated=truncated,
        unsafe_entries=tuple(unsafe),
        warnings=tuple(warnings),
    )


def inspect_archive(path: Path, *, display_path: str) -> ArchiveInspection:
    """Safely inspect a ZIP or TAR archive without extracting anything."""
    suffix = path.suffix.lower()
    name = path.name.lower()
    if zipfile.is_zipfile(path):
        return inspect_zip(path, display_path=display_path)
    is_tar_name = suffix in {".tar", ".tgz"} or name.endswith((".tar.gz", ".tar.bz2", ".tar.xz"))
    if is_tar_name or tarfile.is_tarfile(path):
        return inspect_tar(path, display_path=display_path)
    raise ValidationError(
        "unsupported archive format", details={"path": display_path, "supported": ["zip", "tar"]}
    )


__all__ = [
    "DEFAULT_MAX_ARCHIVE_BYTES",
    "ArchiveEntry",
    "ArchiveInspection",
    "SecretFinding",
    "inspect_archive",
    "load_document",
    "scan_for_secrets",
    "scan_text_for_secrets",
]
