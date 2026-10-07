"""Controlled local command execution.

The local runtime must not hand the agent an unrestricted shell. This module
defines the *architecture* for running commands safely, without granting
unrestricted OS access:

* **Allow-list** — only commands the operator explicitly allowed may run. The
  default allow-list is empty, so nothing runs until it is opted into.
* **Working-directory restriction** — the command runs inside an allowed root.
* **Environment restriction** — only a minimal set of variables is passed; API
  keys and tokens never reach the child process.
* **Timeout** — a hard wall-clock limit, bounded by the run limit.
* **Output limits** — stdout/stderr are truncated.
* **Approval** — the tool declares ``RUN_COMMAND`` (high risk), so the existing
  policy gate requires human approval by default.
* **Audit** — the attempt is recorded before the process is spawned.

The runner is a real implementation (it spawns a real process when allowed); it
is the *policy* that keeps it safe. No command is ever executed through a shell,
so shell metacharacters cannot be injected.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..core.errors import PermissionDeniedError, TimeoutError_, ValidationError
from ..core.process import is_windows, process_group_kwargs, terminate_process

#: Environment variables allowed through to a child process. Everything else
#: (credentials, tokens, cloud config) is stripped.
SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "PYTHONPATH"})

#: Maximum bytes of stdout/stderr returned.
MAX_COMMAND_OUTPUT_BYTES = 32 * 1024


class CommandPolicy(BaseModel):
    """Declarative policy for controlled command execution."""

    model_config = ConfigDict(frozen=True)

    #: Commands (bare program names or absolute paths) that may run. Empty means
    #: "nothing may run" — the safe default.
    allowlist: frozenset[str] = frozenset()
    #: If True, any command not on the allow-list is refused. Always true in
    #: practice; kept explicit so the intent is legible.
    deny_by_default: bool = True
    #: Directories a command's working directory must fall under.
    allowed_cwd_roots: tuple[str, ...] = ()
    #: Hard timeout in seconds.
    timeout_seconds: float = 30.0
    #: Maximum output bytes returned to the model.
    max_output_bytes: int = MAX_COMMAND_OUTPUT_BYTES

    def resolve_program(self, argv: list[str]) -> str:
        """Return the allowed program for ``argv`` or raise.

        Compares both the raw first token and its basename, so an allow-list
        entry of ``git`` matches ``/usr/bin/git``.
        """
        program = argv[0]
        basename = Path(program).name
        if program in self.allowlist or basename in self.allowlist:
            return program
        raise PermissionDeniedError(
            "command is not on the local allow-list",
            details={"program": basename, "allowed": sorted(self.allowlist)},
        )

    def resolve_cwd(self, requested: str | None, *, default: str) -> str:
        cwd = requested or default
        resolved = Path(cwd).resolve()
        configured = tuple(Path(r).resolve() for r in self.allowed_cwd_roots)
        roots = configured or (Path(default).resolve(),)
        if not any(resolved == root or root in resolved.parents for root in roots):
            raise PermissionDeniedError(
                "working directory is outside the allowed roots",
                details={"cwd": str(resolved)},
            )
        return str(resolved)


class CommandResult(BaseModel):
    """Outcome of a controlled command execution."""

    model_config = ConfigDict(frozen=True)

    exit_code: int
    stdout: str
    stderr: str
    argv: tuple[str, ...]
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class ControlledCommandRunner:
    """Spawns allow-listed commands under a policy."""

    def __init__(self, policy: CommandPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> CommandPolicy:
        return self._policy

    @staticmethod
    def _safe_env() -> dict[str, str]:
        return {key: os.environ[key] for key in SAFE_ENV_KEYS if key in os.environ}

    def parse(self, command: str) -> list[str]:
        """Parse a command line into argv *without* shell interpretation.

        The splitter is POSIX on POSIX and Windows-aware on Windows, so a path
        such as ``C:\\Program Files\\app.exe`` is kept as one token rather than
        being split at the backslashes. No shell is ever involved, so shell
        metacharacters (``&``, ``|``, ``;``, ``>``, ``%``, ``!``) remain literal
        data and cannot inject a second command on either platform.
        """
        if not isinstance(command, str) or not command.strip():
            raise ValidationError("command must be a non-empty string")
        try:
            argv = shlex.split(command, posix=not is_windows())
        except ValueError as exc:
            raise ValidationError(f"could not parse command: {exc}") from exc
        if not argv:
            raise ValidationError("command resolved to an empty argv")
        return argv

    async def run(
        self,
        command: str,
        *,
        cwd: str,
        requested_cwd: str | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        """Run ``command`` if policy allows; raise on refusal or timeout."""
        argv = self.parse(command)
        program = self._policy.resolve_program(argv)
        resolved_cwd = self._policy.resolve_cwd(requested_cwd, default=cwd)
        timeout = min(
            float(timeout_seconds or self._policy.timeout_seconds),
            self._policy.timeout_seconds,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=resolved_cwd,
            env=self._safe_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **process_group_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            await terminate_process(process)
            raise TimeoutError_(
                f"command exceeded {timeout}s", details={"program": program}
            ) from exc

        limit = self._policy.max_output_bytes
        truncated = len(stdout) > limit or len(stderr) > limit
        return CommandResult(
            exit_code=process.returncode or 0,
            stdout=stdout[:limit].decode("utf-8", errors="replace"),
            stderr=stderr[:limit].decode("utf-8", errors="replace"),
            argv=tuple(argv),
            truncated=truncated,
        )
