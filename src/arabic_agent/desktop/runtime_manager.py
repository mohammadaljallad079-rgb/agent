"""Desktop runtime lifecycle manager.

A normal Windows user must not open a terminal to start the agent. The desktop
shell uses this manager to launch the local runtime as a **child process**, wait
until it is actually ready, and supervise it:

    double-click the app
      → manager starts the runtime on a loopback port with a fresh control token
      → manager polls readiness until the runtime answers
      → the shell loads the UI, which talks to the runtime over the control API

Running the runtime out-of-process is deliberate:

* a native crash in the model backend cannot take the UI down;
* shutdown is deterministic (terminate the child, then its tree);
* the token never leaves this process except through the child's environment and
  the shell's own authenticated requests.

The manager never binds a public interface: the runtime is started with
``AAP_LOCAL_CONTROL_HOST=127.0.0.1`` on a free loopback port.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

from ..core.process import process_group_kwargs, terminate_process
from .product_paths import resolve_product_paths

logger = logging.getLogger(__name__)

#: How long to wait for the runtime to become ready before giving up (seconds).
DEFAULT_READY_TIMEOUT = 90.0
#: Poll interval while waiting for readiness (seconds).
_READY_POLL = 0.25


class RuntimeStartError(RuntimeError):
    """The runtime process could not be started or never became ready."""


@dataclass
class RuntimeHandle:
    """A running local runtime: where it is and how to talk to it."""

    process: subprocess.Popen[bytes]
    base_url: str
    token: str
    port: int


def _free_loopback_port() -> int:
    """Ask the OS for a free port on the loopback interface."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def runtime_command() -> list[str]:
    """The command that starts the runtime as a module (portable, no console script).

    In a frozen (PyInstaller) build there is no separate interpreter to run
    ``-m`` with, so the product executable re-invokes itself with a ``runtime``
    subcommand; the packaged entry point dispatches on it.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "runtime"]
    return [sys.executable, "-m", "arabic_agent.local.main"]


class DesktopRuntimeManager:
    """Starts, supervises and stops the local agent runtime."""

    def __init__(
        self,
        *,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        extra_env: dict[str, str] | None = None,
        command: list[str] | None = None,
        use_product_paths: bool = True,
    ) -> None:
        self._ready_timeout = ready_timeout
        self._extra_env = dict(extra_env or {})
        self._command = command or runtime_command()
        self._use_product_paths = use_product_paths
        self._handle: RuntimeHandle | None = None

    @property
    def handle(self) -> RuntimeHandle | None:
        return self._handle

    @property
    def base_url(self) -> str:
        if self._handle is None:
            raise RuntimeStartError("the runtime is not running")
        return self._handle.base_url

    @property
    def token(self) -> str:
        if self._handle is None:
            raise RuntimeStartError("the runtime is not running")
        return self._handle.token

    def is_running(self) -> bool:
        return self._handle is not None and self._handle.process.poll() is None

    def _env(self, *, port: int, token: str) -> dict[str, str]:
        env = dict(os.environ)
        env.update(
            {
                "AAP_RUNTIME_KIND": "local",
                "AAP_LOCAL_CONTROL_ENABLED": "true",
                "AAP_LOCAL_CONTROL_HOST": "127.0.0.1",
                "AAP_LOCAL_CONTROL_PORT": str(port),
                "AAP_LOCAL_CONTROL_TOKEN": token,
            }
        )
        # Point the child at the per-user product directories unless the caller
        # (a test, or an operator who set them explicitly) already decided.
        if self._use_product_paths:
            env.update(resolve_product_paths().ensure().env_overrides())
        env.update(self._extra_env)
        return env

    async def start(self, *, token: str | None = None, port: int | None = None) -> RuntimeHandle:
        """Start the runtime and wait until it reports ready."""
        if self.is_running():
            assert self._handle is not None
            return self._handle
        chosen_port = port or _free_loopback_port()
        chosen_token = token or secrets.token_urlsafe(32)
        env = self._env(port=chosen_port, token=chosen_token)
        try:
            process = subprocess.Popen(  # noqa: S603, ASYNC220 - fixed module invocation, no shell
                self._command,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                **process_group_kwargs(),
            )
        except OSError as exc:
            raise RuntimeStartError(f"could not start the runtime: {exc}") from exc
        handle = RuntimeHandle(
            process=process,
            base_url=f"http://127.0.0.1:{chosen_port}",
            token=chosen_token,
            port=chosen_port,
        )
        self._handle = handle
        ready = await self.wait_ready(handle)
        if not ready:
            code = process.poll()
            await self.stop()
            raise RuntimeStartError(
                f"the runtime did not become ready within {self._ready_timeout:.0f}s"
                + (f" (exited with code {code})" if code is not None else "")
            )
        return handle

    async def wait_ready(self, handle: RuntimeHandle) -> bool:
        """Poll the control API until it answers or the timeout elapses."""
        deadline = time.monotonic() + self._ready_timeout
        headers = {"X-Local-Token": handle.token}
        async with httpx.AsyncClient(timeout=2.0) as client:
            while time.monotonic() < deadline:
                if handle.process.poll() is not None:
                    return False
                try:
                    response = await client.get(f"{handle.base_url}/local/status", headers=headers)
                except httpx.HTTPError:
                    await asyncio.sleep(_READY_POLL)
                    continue
                if response.status_code == 200:
                    return True
                await asyncio.sleep(_READY_POLL)
        return False

    async def stop(self, *, grace_seconds: float = 10.0) -> None:
        """Stop the runtime gracefully, then forcefully if it does not exit."""
        handle = self._handle
        if handle is None:
            return
        await terminate_process(handle.process, grace_seconds=grace_seconds)
        self._handle = None

    async def restart(self) -> RuntimeHandle:
        """Stop and start again (used after a detected crash)."""
        await self.stop()
        return await self.start()


__all__ = [
    "DEFAULT_READY_TIMEOUT",
    "DesktopRuntimeManager",
    "RuntimeHandle",
    "RuntimeStartError",
    "runtime_command",
]
