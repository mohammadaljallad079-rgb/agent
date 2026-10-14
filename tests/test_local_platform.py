"""Local-runtime platform integration tests.

These build a real :class:`~arabic_agent.container.Platform` in local mode and
exercise the wiring end to end: the local tools are registered only for a local
runtime, the sandbox policy reaches the tools through the executor, the control
API authenticates and reports real state, and a real local file round-trips
through the executor and its policy gate.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from arabic_agent.container import Platform
from arabic_agent.core.config import Settings
from arabic_agent.core.enums import OperatingMode, RuntimeKind, RuntimeState
from arabic_agent.core.errors import PermissionDeniedError
from arabic_agent.local.control import LocalControlAPI
from arabic_agent.local.http import create_local_app
from arabic_agent.models.registry import ProviderRegistry

from .helpers import FakeProvider, text_response


def _local_settings(tmp_path: Path) -> Settings:
    return Settings(
        environment="test",
        require_auth=False,
        runtime_kind=RuntimeKind.LOCAL,
        local_store_dir=str(tmp_path / "data"),
        local_workspace_roots=(str(tmp_path / "approved"),),
        local_mode=OperatingMode.OFFLINE,
        local_control_token=SecretStr("control-secret"),
        max_steps_per_run=20,
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
    )


def _build_local(tmp_path: Path) -> Platform:
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    return Platform.build(
        _local_settings(tmp_path),
        provider_registry=registry,
        default_model="fake/model",
    )


def test_local_runtime_is_assembled_in_local_mode(tmp_path):
    platform = _build_local(tmp_path)
    assert platform.local_runtime is not None
    assert platform.settings.runtime_kind is RuntimeKind.LOCAL


def test_cloud_runtime_has_no_local_runtime(tmp_path):
    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    settings = Settings(
        environment="test",
        require_auth=False,
        local_store_dir=str(tmp_path / "data"),
        database_url=SecretStr(""),
        redis_url=SecretStr(""),
        execution_mode="inline",
    )
    platform = Platform.build(settings, provider_registry=registry, default_model="fake/model")
    assert platform.local_runtime is None
    # Local tools must be absent on a cloud runtime.
    names = platform.tools.names()
    assert "read_local_file" not in names
    assert "run_local_command" not in names


def test_local_runtime_registers_local_tools(tmp_path):
    platform = _build_local(tmp_path)
    names = platform.tools.names()
    assert "read_local_file" in names
    assert "write_local_file" in names
    assert "run_local_command" in names


async def test_local_runtime_lifecycle(tmp_path):
    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    status = await runtime.start()
    assert status.state is RuntimeState.READY
    status = await runtime.run()
    assert status.state is RuntimeState.RUNNING
    status = await runtime.pause()
    assert status.state is RuntimeState.PAUSED
    with pytest.raises(Exception):
        runtime.assert_accepts_tools()
    status = await runtime.resume()
    assert status.state is RuntimeState.RUNNING
    status = await runtime.stop()
    assert status.state is RuntimeState.STOPPED


async def test_local_runtime_status_reports_capabilities(tmp_path):
    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    status = runtime.status()
    assert "files.read" in status.capabilities
    assert "files.read" in status.available_capabilities
    assert status.workspace_dir
    assert status.allowed_roots


async def test_local_tool_round_trips_through_executor(tmp_path):
    platform = _build_local(tmp_path)
    workspace = tmp_path / "approved"
    workspace.mkdir(parents=True, exist_ok=True)
    from arabic_agent.permissions.base import Principal
    from arabic_agent.tools.base import ToolContext

    principal = Principal(tenant_id="tnt_1", user_id="usr_1")
    context = ToolContext(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        workspace_dir=str(workspace),
    )
    # FILES_WRITE requires approval by default; use the approved path to write.
    outcome = await platform.executor.execute(
        principal=principal,
        tool_name="write_local_file",
        arguments={"path": "hello.txt", "content": "مرحبا"},
        context=context,
    )
    assert outcome.kind.value == "approval_required"

    approval = outcome.approval
    assert approval is not None
    from arabic_agent.core.enums import ApprovalStatus

    await platform.approvals.resolve(
        approval.id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    approved = await platform.approvals.get(approval.id, tenant_id="tnt_1")
    result = await platform.executor.execute_approved(
        approval=approved, context=context, principal=principal
    )
    assert result.kind.value == "completed"
    assert (workspace / "hello.txt").read_text(encoding="utf-8") == "مرحبا"

    # A read needs no approval.
    read = await platform.executor.execute(
        principal=principal,
        tool_name="read_local_file",
        arguments={"path": "hello.txt"},
        context=context,
    )
    assert read.kind.value == "completed"


async def test_local_tool_cannot_escape_sandbox(tmp_path):
    platform = _build_local(tmp_path)
    workspace = tmp_path / "approved"
    workspace.mkdir(parents=True, exist_ok=True)
    from arabic_agent.permissions.base import Principal
    from arabic_agent.tools.base import ToolContext

    principal = Principal(tenant_id="tnt_1", user_id="usr_1")
    context = ToolContext(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        workspace_dir=str(workspace),
    )
    with pytest.raises(PermissionDeniedError):
        await platform.executor.execute(
            principal=principal,
            tool_name="read_local_file",
            arguments={"path": "../../../etc/passwd"},
            context=context,
        )


async def test_document_generation_round_trips_through_executor(tmp_path):
    """A real PDF with Arabic is written through policy → approval → sandbox.

    This proves the generation tools are registered, gated by ``WRITE_FILE``
    (approval-required), and write into the workspace — the same path every
    other write takes, not a side door.
    """
    from arabic_agent.core.enums import ApprovalStatus
    from arabic_agent.permissions.base import Principal
    from arabic_agent.tools.base import ToolContext

    # The tool itself reports PDF generation unavailable without reportlab; the
    # round-trip can only be asserted when the docgen extra is installed.
    pytest.importorskip("reportlab")
    platform = _build_local(tmp_path)
    workspace = tmp_path / "approved"
    workspace.mkdir(parents=True, exist_ok=True)
    principal = Principal(tenant_id="tnt_1", user_id="usr_1")
    context = ToolContext(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        workspace_dir=str(workspace),
    )

    outcome = await platform.executor.execute(
        principal=principal,
        tool_name="generate_document",
        arguments={
            "path": "تقرير.pdf",
            "title": "تقرير",
            "blocks": [{"type": "paragraph", "text": "مرحبا بالعالم"}],
        },
        context=context,
    )
    assert outcome.kind.value == "approval_required"

    approval = outcome.approval
    assert approval is not None
    await platform.approvals.resolve(
        approval.id,
        tenant_id="tnt_1",
        status=ApprovalStatus.APPROVED,
        resolved_by="usr_1",
    )
    approved = await platform.approvals.get(approval.id, tenant_id="tnt_1")
    result = await platform.executor.execute_approved(
        approval=approved, context=context, principal=principal
    )
    assert result.kind.value == "completed"
    written = workspace / "تقرير.pdf"
    assert written.is_file()
    assert written.stat().st_size > 0
    assert written.read_bytes().startswith(b"%PDF")


def test_control_api_auth_fails_closed_without_token(tmp_path):
    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    # With auth required and no token configured, the surface refuses everyone.
    strict_settings = _local_settings(tmp_path).model_copy(
        update={"local_control_token": SecretStr(""), "require_auth": True}
    )
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=strict_settings)
    assert not control.auth_configured
    with pytest.raises(PermissionDeniedError):
        control.authorize("anything")


def test_control_api_accepts_valid_token(tmp_path):
    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=platform.settings)
    control.authorize("control-secret")
    with pytest.raises(PermissionDeniedError):
        control.authorize("wrong")


async def test_control_snapshot_reports_real_state(tmp_path):
    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=platform.settings)
    snapshot = await control.snapshot(tenant_id="local")
    assert snapshot.runtime["state"] == "stopped"
    assert snapshot.pending_approvals == 0
    assert snapshot.local_model["available"] is False
    assert "read_local_file" in snapshot.tools


def test_local_http_app_requires_token(tmp_path):
    from fastapi.testclient import TestClient

    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=platform.settings)
    app = create_local_app(control, settings=platform.settings, tenant_id="local")
    client = TestClient(app)
    assert client.get("/local/status").status_code == 403
    ok = client.get("/local/status", headers={"X-Local-Token": "control-secret"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["runtime"]["state"] == "stopped"
    assert "read_local_file" in body["tools"]


def test_local_http_app_rejects_invalid_token(tmp_path):
    from fastapi.testclient import TestClient

    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=platform.settings)
    app = create_local_app(control, settings=platform.settings, tenant_id="local")
    client = TestClient(app)
    # A present-but-wrong token is rejected exactly like a missing one.
    resp = client.get("/local/status", headers={"X-Local-Token": "not-the-token"})
    assert resp.status_code == 403
    resp = client.get("/local/capabilities", headers={"X-Local-Token": "not-the-token"})
    assert resp.status_code == 403


def test_local_http_capabilities_are_reported(tmp_path):
    from fastapi.testclient import TestClient

    platform = _build_local(tmp_path)
    runtime = platform.local_runtime
    assert runtime is not None
    control = LocalControlAPI(runtime=runtime, platform=platform, settings=platform.settings)
    app = create_local_app(control, settings=platform.settings, tenant_id="local")
    client = TestClient(app)
    resp = client.get("/local/capabilities", headers={"X-Local-Token": "control-secret"})
    assert resp.status_code == 200
    caps = {c["id"]: c for c in resp.json()}
    assert caps["files.read"]["implemented"] is True
    assert caps["input.control"]["implemented"] is False


def test_desktop_launcher_builds_real_app(tmp_path):
    from fastapi.testclient import TestClient

    from arabic_agent.core.errors import ConfigurationError
    from arabic_agent.local.main import build_local_control_app
    from arabic_agent.models.registry import ProviderRegistry

    registry = ProviderRegistry()
    registry.register(FakeProvider(lambda req, n: text_response("ok")))
    app = build_local_control_app(
        _local_settings(tmp_path), provider_registry=registry, default_model="fake/model"
    )
    client = TestClient(app)
    assert client.get("/local/status").status_code == 403
    ok = client.get("/local/status", headers={"X-Local-Token": "control-secret"})
    assert ok.status_code == 200

    # Without a control token, the launcher refuses to start (fail closed).
    no_token = _local_settings(tmp_path).model_copy(
        update={"local_control_token": SecretStr(""), "require_auth": True}
    )
    with pytest.raises(ConfigurationError):
        build_local_control_app(no_token, provider_registry=registry, default_model="fake/model")
