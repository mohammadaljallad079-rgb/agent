"""Offline isolation: the local runtime works with no network at all.

Phase 4B acceptance test. The strongest claim an offline-first product can make
is that the agent completes a real task while the machine has **no network**.
We do not simulate that: the agent's work runs inside a Linux network namespace
with only the loopback interface, where any outbound connection fails at the OS
level. The test skips itself (rather than passing vacuously) when the sandbox
cannot create a namespace, so a green run always means the isolation was real.

The namespace wraps only the *isolated work*, not the whole pytest process, so
the host tooling is unaffected.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = str(Path(__file__).resolve().parents[1])


def _isolation_prefix() -> list[str] | None:
    """Command prefix that runs a child with no network, or None if unavailable.

    ``unshare -n`` needs CAP_SYS_ADMIN, so as a non-root user we go through
    passwordless sudo; if that is not available the test skips instead of
    pretending to be offline.
    """
    if not shutil.which("unshare"):
        return None
    if os.geteuid() == 0:
        return ["unshare", "-n"]
    if shutil.which("sudo"):
        probe = subprocess.run(["/usr/bin/sudo", "-n", "true"], capture_output=True, text=True)
        if probe.returncode == 0:
            return ["sudo", "-n", "unshare", "-n"]
    return None


_ISOLATED_SCRIPT = textwrap.dedent(
    """
    import asyncio, json, socket, sys
    from pydantic import SecretStr
    from arabic_agent.container import Platform
    from arabic_agent.core.config import Settings
    from arabic_agent.core.enums import (
        NetworkPolicy, OperatingMode, RuntimeKind,
    )
    from arabic_agent.local.boundary import DataBoundary
    from arabic_agent.models.registry import ProviderRegistry
    from arabic_agent.permissions.base import Principal
    from arabic_agent.runs.base import RunRecord
    from tests.helpers import (
        FakeProvider, plan_json, scripted_responder, verify_json,
    )

    store_dir, workspace = sys.argv[1], sys.argv[2]

    # 1) Prove the OS really has no route out before doing anything else.
    try:
        s = socket.socket()
        s.settimeout(2.0)
        s.connect(("1.1.1.1", 80))
        s.close()
        print(json.dumps({"error": "network is reachable; isolation failed"}))
        sys.exit(3)
    except OSError:
        pass

    settings = Settings(
        environment="test", require_auth=False, runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=store_dir, local_workspace_roots=(workspace,),
        local_mode=OperatingMode.OFFLINE, local_network_policy=NetworkPolicy.DISABLED,
        database_url=SecretStr(""), redis_url=SecretStr(""), execution_mode="inline",
        max_steps_per_run=20,
    )
    registry = ProviderRegistry()
    registry.register(FakeProvider(scripted_responder(
        plan=plan_json(("echo", "echo")),
        tool_calls=[("echo", {"text": "x"}), ("finish", {"result": "تم"})],
        verification=verify_json(True, "تم"),
    )))
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")

    async def main():
        principal = Principal(tenant_id="tnt_1", user_id="usr_1")
        # 2) The agent runs a real goal end to end, fully offline.
        record = await platform.runs.create(
            RunRecord(
                tenant_id="tnt_1", user_id="usr_1", goal="اكتب تقريرا",
                workspace_dir=workspace,
            )
        )
        result = await platform.build_runner().run(
            principal=principal, goal="اكتب تقريرا",
            workspace_dir=workspace, run_id=record.id,
        )
        # 3) Offline policy refuses every outbound host at the boundary.
        decision = await platform.local_runtime.authorize_network("api.openai.com")
        print(json.dumps({
            "status": result.status.value,
            "network_allowed": decision.allowed,
            "routed_to": decision.routed_to.value,
            "boundary_local": DataBoundary.LOCAL.value,
        }))
        await platform.aclose()

    asyncio.run(main())
    """
)


@pytest.mark.skipif(_isolation_prefix() is None, reason="no unshare/sudo for a network namespace")
def test_agent_completes_offline_under_real_network_isolation(tmp_path):
    store = str(tmp_path / "data")
    workspace = str(tmp_path / "approved")
    Path(workspace).mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "PYTHONPATH": REPO_ROOT}
    try:
        proc = subprocess.run(  # noqa: S603 - fixed interpreter and literal script
            [*_isolation_prefix(), sys.executable, "-c", _ISOLATED_SCRIPT, store, workspace],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=env,
            timeout=180,
        )
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        assert payload["status"] == "completed", payload
        assert payload["network_allowed"] is False
        # A denied outbound request is still classified as remote-bound; the
        # point is that it is refused, never sent.
        assert payload["routed_to"] == "remote"
    finally:
        # The isolated child runs as root, so its store is root-owned; remove it
        # with the same privilege so the test leaves nothing behind.
        if os.geteuid() != 0:
            subprocess.run(  # noqa: S603 - fixed binary, literal args
                ["/usr/bin/sudo", "-n", "rm", "-rf", store], capture_output=True, text=True
            )
        else:
            shutil.rmtree(store, ignore_errors=True)
