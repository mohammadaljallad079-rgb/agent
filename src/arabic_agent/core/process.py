"""Portable child-process termination.

Child processes are stopped differently on each platform, and getting it wrong
leaves orphaned trees or hangs. POSIX uses signals and process groups; Windows
has no signals for processes at all — ``terminate()``/``kill()`` map to
``TerminateProcess`` and do **not** touch grandchildren. This module is the one
place that knows those differences, so every other module terminates a process
the same way.

The two entry points are:

* :func:`terminate_process` — stop a single ``asyncio`` child (graceful, then
  forceful) with a bounded wait, never raising on an already-dead child;
* :func:`process_group_kwargs` — the ``subprocess``/``create_subprocess_exec``
  keyword arguments that put a child in its own group/session so a whole tree
  can be signalled at once (``start_new_session`` on POSIX; a new process group
  on Windows).

Nothing here imports a platform module at module import time, so the package
still imports on any OS.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import sys
from typing import Any

logger = logging.getLogger(__name__)

#: How long to wait for a graceful stop before escalating (seconds).
DEFAULT_GRACE_SECONDS = 5.0
#: How long to wait for a forced stop before giving up (seconds).
DEFAULT_KILL_SECONDS = 5.0


def is_windows() -> bool:
    """Whether this process is running on Windows."""
    return sys.platform.startswith("win")


def process_group_kwargs() -> dict[str, Any]:
    """Arguments that isolate a child in its own group/session.

    Passed to ``asyncio.create_subprocess_exec`` / ``subprocess.Popen`` so that:

    * on POSIX, a whole tree can be signalled via the child's process group
      (``os.killpg``) and survives a ``Ctrl-C`` to the parent;
    * on Windows, the child leads its own process group so it can receive a
      console control event and can be torn down with ``taskkill /T``.

    ``start_new_session`` is a POSIX-only keyword; ``creationflags`` is a
    Windows-only keyword. Each is supplied only on its own platform so neither
    platform rejects an unknown argument.
    """
    if is_windows():
        import subprocess

        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0))
        return {"creationflags": flags}
    return {"start_new_session": True}


def _kill_windows_tree(pid: int) -> None:
    """Force-kill a Windows process and its children via ``taskkill /T /F``.

    ``taskkill`` is a fixed Windows system binary invoked with a structured
    argv (never a shell); ``pid`` is an integer, so there is nothing to inject.
    """
    import shutil
    import subprocess

    taskkill = shutil.which("taskkill")
    if not taskkill:  # pragma: no cover - Windows only
        return
    try:
        subprocess.run(  # noqa: S603 - resolved system binary, integer pid, no shell
            [taskkill, "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
            timeout=DEFAULT_KILL_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - Windows only
        logger.warning("taskkill failed for pid %s: %s", pid, exc)


def _is_running(process: Any) -> bool:
    """Whether a child is still running, for both ``asyncio`` and blocking APIs."""
    if isinstance(process, asyncio.subprocess.Process):
        return process.returncode is None
    return process.poll() is None


async def _await_exit(process: Any, seconds: float) -> None:
    """Wait for a child to exit, whichever process type it is."""
    if isinstance(process, asyncio.subprocess.Process):
        await asyncio.wait_for(process.wait(), timeout=seconds)
    else:
        await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=seconds)


async def terminate_process(
    process: Any,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    kill_seconds: float = DEFAULT_KILL_SECONDS,
) -> None:
    """Stop ``process`` gracefully, then forcefully; never raise.

    Works for both ``asyncio.subprocess.Process`` and ``subprocess.Popen`` (both
    expose ``terminate``/``kill``; the exit check differs, so it is abstracted).
    On Windows the child tree is additionally torn down with ``taskkill /T /F``
    because ``kill()`` only stops the direct child. A process that has already
    exited is a no-op.
    """
    if not _is_running(process):
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        process.terminate()
    try:
        await _await_exit(process, grace_seconds)
        return
    except TimeoutError:
        pass
    except (ProcessLookupError, OSError):
        return

    pid = getattr(process, "pid", None)
    if is_windows() and isinstance(pid, int):
        await asyncio.to_thread(_kill_windows_tree, pid)
    with contextlib.suppress(ProcessLookupError, OSError):
        process.kill()
    with contextlib.suppress(TimeoutError, ProcessLookupError, OSError):
        await _await_exit(process, kill_seconds)


def terminate_tree_sync(process: Any, *, grace_seconds: float = DEFAULT_GRACE_SECONDS) -> None:
    """Blocking variant of :func:`terminate_process` for non-async callers."""
    if process.poll() is not None:
        return
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.terminate()
        process.wait(timeout=grace_seconds)
        return
    pid = getattr(process, "pid", None)
    if is_windows() and isinstance(pid, int):
        _kill_windows_tree(pid)
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        process.kill()
        process.wait(timeout=DEFAULT_KILL_SECONDS)


__all__ = [
    "DEFAULT_GRACE_SECONDS",
    "DEFAULT_KILL_SECONDS",
    "is_windows",
    "process_group_kwargs",
    "terminate_process",
    "terminate_tree_sync",
]
