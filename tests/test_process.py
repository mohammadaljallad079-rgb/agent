"""Portable process termination tests.

The Windows-specific branch (``taskkill /T /F``) cannot run off Windows, but the
behaviour it shares with POSIX — graceful-then-forceful stop, never raising on a
dead child, process-group isolation — is exercised with real child processes.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

from arabic_agent.core import process as process_mod
from arabic_agent.core.process import (
    is_windows,
    process_group_kwargs,
    terminate_process,
    terminate_tree_sync,
)


def test_is_windows_matches_platform():
    assert is_windows() == sys.platform.startswith("win")


def test_process_group_kwargs_shape():
    kwargs = process_group_kwargs()
    if is_windows():
        assert "creationflags" in kwargs and "start_new_session" not in kwargs
    else:
        assert kwargs == {"start_new_session": True}


async def test_terminate_process_stops_a_running_child():
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        **process_group_kwargs(),
    )
    assert child.returncode is None
    await terminate_process(child)
    assert child.returncode is not None


async def test_terminate_process_is_noop_on_a_finished_child():
    child = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await child.wait()
    assert child.returncode == 0
    # Must not raise for an already-exited process.
    await terminate_process(child)


def test_terminate_tree_sync_stops_a_running_child():
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        **process_group_kwargs(),
    )
    assert child.poll() is None
    terminate_tree_sync(child)
    assert child.poll() is not None


def test_terminate_tree_sync_is_noop_on_finished_child():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    terminate_tree_sync(child)
    assert child.returncode == 0


async def test_windows_branch_escalates_to_tree_kill(monkeypatch):
    """On Windows, a child that ignores termination is tree-killed, not just
    ``kill()``-ed (which would leave grandchildren alive)."""
    recorded: list[int] = []

    def fake_tree_kill(pid: int) -> None:
        recorded.append(pid)

    monkeypatch.setattr(process_mod, "is_windows", lambda: True)
    monkeypatch.setattr(process_mod, "_kill_windows_tree", fake_tree_kill)

    async def never_exit(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr(process_mod, "_await_exit", never_exit)

    killed: list[bool] = []

    class _Child:
        pid = 4242
        returncode = None

        def terminate(self):
            pass

        def kill(self):
            killed.append(True)

        def poll(self):
            return None

    await terminate_process(_Child(), grace_seconds=0.01, kill_seconds=0.01)
    assert recorded == [4242]
    assert killed == [True]
