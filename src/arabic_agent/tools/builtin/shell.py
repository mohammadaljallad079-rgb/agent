"""Shell command tool.

Runs a command in the workspace directory with a hard timeout, a minimal
environment (no inherited secrets), and a shell-free argv interface so a model
cannot inject shell metacharacters. This tool is high risk and, under the
default policy, always requires human approval.
"""

from __future__ import annotations

import asyncio
import os
import shlex
from typing import Any, ClassVar

from ...core.errors import TimeoutError_, ValidationError
from ...permissions.base import Permission
from ..base import Tool, ToolContext, ToolResult

MAX_OUTPUT_BYTES = 32 * 1024

#: Environment variables allowed through to child processes. Everything else
#: (API keys, tokens) is stripped so a command cannot exfiltrate secrets.
_SAFE_ENV_KEYS = frozenset({"PATH", "HOME", "LANG", "LC_ALL", "TZ", "TERM", "PYTHONPATH"})


class ShellCommandTool(Tool):
    name = "run_command"
    description = (
        "Run a shell command inside the workspace directory. Provide the command "
        "as an argv array (no shell interpretation). Output is truncated."
    )
    parameters: ClassVar[dict[str, Any]] = {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Command line to execute (parsed with shell-like quoting)",
            },
            "timeout_seconds": {
                "type": "number",
                "description": "Optional timeout in seconds (bounded by the run limit)",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    }
    permissions = (Permission.RUN_COMMAND,)

    def _safe_env(self) -> dict[str, str]:
        return {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}

    async def run(self, arguments: dict[str, Any], context: ToolContext) -> ToolResult:
        command = arguments["command"]
        if not isinstance(command, str) or not command.strip():
            raise ValidationError("command must be a non-empty string")
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise ValidationError(f"could not parse command: {exc}") from exc
        if not argv:
            raise ValidationError("command resolved to an empty argv")

        timeout = min(
            float(arguments.get("timeout_seconds") or context.timeout_seconds),
            context.timeout_seconds,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=context.workspace_dir,
            env=self._safe_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise TimeoutError_(
                f"command exceeded {timeout}s", details={"command": command}
            ) from exc

        out = stdout[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        err = stderr[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        exit_code = process.returncode or 0
        ok = exit_code == 0
        summary = f"command exited with code {exit_code}"
        data = {"exit_code": exit_code, "stdout": out, "stderr": err, "argv": argv}
        return (
            ToolResult.success(summary, data=data) if ok else ToolResult.failure(summary, data=data)
        )
