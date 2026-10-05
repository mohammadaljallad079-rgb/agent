"""Filesystem sandbox for the local runtime.

This is the security boundary between the agent and the user's computer. It is
the *local* counterpart to ``tools/builtin/_paths.py`` and is deliberately
stricter, because on a real machine the stakes are higher: the process can see
the whole filesystem, not just a container.

Threat model and defences
-------------------------
* **Traversal** (``../../etc/passwd``): every requested path is normalised and
  its parts inspected; ``..`` is rejected outright rather than resolved.
* **Absolute-path bypass** (``/etc/shadow``): absolute paths are resolved and
  then re-checked against the allow-list; they are only permitted when they
  fall under an explicitly allowed root.
* **Symlink escape**: the *real* path (``Path.resolve``) of the target and of
  every ancestor that exists is compared against the allowed roots, so a
  symlink pointing outside the workspace is refused even though the textual
  path looked safe. A symlink inside the workspace that points outside it is
  also refused.
* **Sensitive-location protection**: an explicit deny-list (``/etc``, ``/proc``,
  ``/sys``, SSH keys, cloud credential directories, ...) is refused *before*
  the allow-list is consulted, so an over-broad allow root cannot expose them.
* **Workspace escape**: the default policy allows exactly one root — the run's
  workspace — and nothing else.

The sandbox answers two questions and nothing else:

* :meth:`FilesystemSandbox.authorize_read` — may this path be read?
* :meth:`FilesystemSandbox.authorize_write` — may this path be written?

Both return a :class:`SandboxDecision`; neither performs I/O. Tools perform the
I/O after the decision, so the policy is testable in isolation and every call
site is auditable.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..core.errors import PermissionDeniedError, ValidationError


class SandboxDecision(BaseModel):
    """The result of a sandbox authorisation check."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    reason: str
    #: The canonical (fully resolved) path when allowed, else ``None``.
    resolved: str | None = None
    #: The rule that produced the decision, for audit.
    rule: str = ""

    @classmethod
    def allow(cls, resolved: str, *, rule: str) -> SandboxDecision:
        return cls(allowed=True, reason=f"allowed by {rule}", resolved=resolved, rule=rule)

    @classmethod
    def deny(cls, reason: str, *, rule: str) -> SandboxDecision:
        return cls(allowed=False, reason=reason, rule=rule)


#: Directory names that are always refused, at any depth. Compared
#: case-insensitively on the path *parts*, never as substrings, so a directory
#: called ``/home/me/etcetera`` is not accidentally denied.
_SENSITIVE_DIR_NAMES = frozenset(
    {
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".gcloud",
        ".kube",
        ".docker",
        ".config",
        "keychains",
        "credentials",
    }
)

#: Absolute paths (or path prefixes) that are always refused.
_SENSITIVE_PREFIXES: tuple[str, ...] = (
    "/etc",
    "/proc",
    "/sys",
    "/dev",
    "/boot",
    "/root",
    "/var/lib",
    "/var/run",
    "/run",
    "/private/etc",
    "/private/var",
    "/system",
    "/library/keychains",
    "c:\\windows",
    "c:\\program files",
    "c:\\programdata",
)

#: File-name patterns that are always refused (matched case-insensitively).
_SENSITIVE_FILE_SUFFIXES: tuple[str, ...] = (
    ".pem",
    ".key",
    ".pfx",
    ".p12",
    ".jks",
    ".keystore",
)
_SENSITIVE_FILE_NAMES: frozenset[str] = frozenset(
    {
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "authorized_keys",
        "known_hosts",
        ".netrc",
        ".pgpass",
        "credentials",
        "shadow",
        "passwd",
    }
)

#: Refuse to write files larger than this by default (guards against a runaway
#: tool filling the disk). Callers may raise it explicitly.
DEFAULT_MAX_WRITE_BYTES = 64 * 1024 * 1024


class FilesystemPolicy(BaseModel):
    """Declarative filesystem access policy for one local runtime.

    ``allowed_roots`` is an allow-list of directories the agent may touch.
    ``denied_roots`` is an explicit deny-list applied first. A path must be
    under an allowed root and not under any denied root, and must not match the
    built-in sensitive-location rules.
    """

    model_config = ConfigDict(frozen=True)

    #: Directories the agent may access. Defaults to the single run workspace.
    allowed_roots: tuple[str, ...] = ()
    #: The directory relative paths are anchored to. Set by ``workspace_only``;
    #: an absolute path is still required to be under an allowed root.
    workspace_dir: str = ""
    #: Directories explicitly refused, even if under an allowed root.
    denied_roots: tuple[str, ...] = ()
    #: Whether reads are permitted at all (a write-only drop box is valid).
    allow_read: bool = True
    #: Whether writes are permitted at all.
    allow_write: bool = True
    #: Whether symlinks that resolve outside an allowed root are refused.
    follow_symlinks: bool = False
    #: Maximum size, in bytes, of a file the agent may write.
    max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES

    @classmethod
    def workspace_only(cls, workspace_dir: str) -> FilesystemPolicy:
        """The safe default: exactly one allowed root (the run workspace)."""
        return cls(allowed_roots=(workspace_dir,), workspace_dir=workspace_dir)

    def canonical_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for raw in self.allowed_roots:
            if not raw or not raw.strip():
                continue
            roots.append(Path(raw).expanduser().resolve())
        return tuple(roots)

    def canonical_denied(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for raw in self.denied_roots:
            if not raw or not raw.strip():
                continue
            roots.append(Path(raw).expanduser().resolve())
        return tuple(roots)


class FilesystemSandbox:
    """Authorises filesystem paths against a :class:`FilesystemPolicy`.

    The sandbox never performs I/O and never mutates the filesystem. It only
    answers allow/deny for a concrete path, which keeps the security logic
    auditable and unit-testable without touching the real disk.
    """

    def __init__(self, policy: FilesystemPolicy) -> None:
        if not policy.allowed_roots:
            raise ValidationError(
                "a filesystem policy must declare at least one allowed root; "
                "an empty allow-list would grant nothing (never everything)"
            )
        self._policy = policy
        self._roots = policy.canonical_roots()
        self._denied = policy.canonical_denied()
        self._workspace = (
            Path(policy.workspace_dir).expanduser().resolve()
            if policy.workspace_dir
            else (self._roots[0] if self._roots else None)
        )

    @property
    def policy(self) -> FilesystemPolicy:
        return self._policy

    @property
    def allowed_roots(self) -> tuple[Path, ...]:
        return self._roots

    # -- Public API -----------------------------------------------------
    def authorize_read(self, requested: str) -> SandboxDecision:
        """Decide whether ``requested`` may be read."""
        if not self._policy.allow_read:
            return SandboxDecision.deny("reads are disabled by policy", rule="read-disabled")
        return self._authorize(requested, write=False)

    def authorize_write(self, requested: str) -> SandboxDecision:
        """Decide whether ``requested`` may be written."""
        if not self._policy.allow_write:
            return SandboxDecision.deny("writes are disabled by policy", rule="write-disabled")
        return self._authorize(requested, write=True)

    def resolve_for_read(self, requested: str) -> Path:
        """Return the canonical path or raise :class:`PermissionDeniedError`."""
        decision = self.authorize_read(requested)
        if not decision.allowed or decision.resolved is None:
            raise PermissionDeniedError(decision.reason, details={"path": requested})
        return Path(decision.resolved)

    def resolve_for_write(self, requested: str) -> Path:
        """Return the canonical path or raise :class:`PermissionDeniedError`."""
        decision = self.authorize_write(requested)
        if not decision.allowed or decision.resolved is None:
            raise PermissionDeniedError(decision.reason, details={"path": requested})
        return Path(decision.resolved)

    # -- Internals ------------------------------------------------------
    def _authorize(self, requested: str, *, write: bool) -> SandboxDecision:
        if not requested or not requested.strip():
            return SandboxDecision.deny("path must not be empty", rule="empty-path")

        expanded = Path(requested).expanduser()
        # Normalise ``.`` and ``..`` *textually first* so a traversal attempt is
        # refused on sight rather than silently resolved to a sibling.
        if ".." in expanded.parts:
            return SandboxDecision.deny(
                "parent-directory traversal is not permitted", rule="traversal"
            )

        # A NUL byte can truncate a path at the syscall boundary; refuse it.
        if "\x00" in requested:
            return SandboxDecision.deny("path contains a NUL byte", rule="nul-byte")

        # Relative paths are anchored to the workspace root (the primary allowed
        # root), not the process CWD, so tool paths are deterministic. Absolute
        # paths are used as-is and still re-checked against the allow-list below.
        if not expanded.is_absolute() and self._workspace is not None:
            expanded = self._workspace / expanded

        try:
            resolved = expanded.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # pragma: no cover - platform dependent
            return SandboxDecision.deny(f"path could not be resolved: {exc}", rule="resolve-error")

        # Sensitive-location checks run *before* the allow-list so an
        # over-broad allowed root cannot expose them.
        sensitive = self._sensitive_reason(resolved)
        if sensitive is not None:
            return SandboxDecision.deny(sensitive, rule="sensitive-location")

        # Deny-list (explicit) beats the allow-list.
        for denied in self._denied:
            if _is_within(resolved, denied):
                return SandboxDecision.deny(
                    f"path is under a denied root: {denied}", rule="denied-root"
                )

        # Must be under an allowed root.
        allowed_root = next((r for r in self._roots if _is_within(resolved, r)), None)
        if allowed_root is None:
            return SandboxDecision.deny(
                "path is outside every allowed root", rule="outside-allowed-root"
            )

        # Symlink protection. If any existing component is a symlink, its real
        # target must also stay inside the *same* allowed root.
        if not self._policy.follow_symlinks:
            escape = self._symlink_escape(resolved, allowed_root)
            if escape is not None:
                return SandboxDecision.deny(escape, rule="symlink-escape")

        return SandboxDecision.allow(str(resolved), rule="write" if write else "read")

    def _sensitive_reason(self, resolved: Path) -> str | None:
        parts_lower = [p.lower() for p in resolved.parts]
        for part in parts_lower:
            if part in _SENSITIVE_DIR_NAMES:
                return f"refused: sensitive directory {part!r}"

        posix = resolved.as_posix().lower()
        for prefix in _SENSITIVE_PREFIXES:
            normalized = prefix.replace("\\", "/")
            if posix == normalized or posix.startswith(normalized.rstrip("/") + "/"):
                return f"refused: sensitive system location {prefix}"

        name = resolved.name.lower()
        if name in _SENSITIVE_FILE_NAMES:
            return f"refused: sensitive file {name!r}"
        if name.endswith(_SENSITIVE_FILE_SUFFIXES):
            return f"refused: sensitive key material {name!r}"
        return None

    def _symlink_escape(self, resolved: Path, allowed_root: Path) -> str | None:
        """Return a denial reason if a symlink component escapes the root.

        Walks from the allowed root down to the target. The first component
        that is a symlink is resolved; if its real target is outside the
        allowed root, the path is refused.
        """
        try:
            relative = resolved.relative_to(allowed_root)
        except ValueError:  # pragma: no cover - guarded by _is_within above
            return "path is outside the allowed root"

        current = allowed_root
        for part in relative.parts:
            current = current / part
            try:
                if current.is_symlink():
                    target = current.resolve(strict=False)
                    if not _is_within(target, allowed_root):
                        return f"refused: symlink {part!r} escapes the sandbox"
            except OSError:  # pragma: no cover - broken symlink races
                return f"refused: could not inspect symlink {part!r}"
        return None


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or a descendant of it."""
    if path == root:
        return True
    return root in path.parents


def is_symlink(path: Path) -> bool:
    """Whether ``path`` is a symbolic link (without following it)."""
    try:
        return stat.S_ISLNK(os.lstat(path).st_mode)
    except OSError:
        return False


#: Fields exported for tests that want to assert the built-in deny rules.
SENSITIVE_DIR_NAMES = _SENSITIVE_DIR_NAMES
SENSITIVE_PREFIXES = _SENSITIVE_PREFIXES
SENSITIVE_FILE_NAMES = _SENSITIVE_FILE_NAMES
