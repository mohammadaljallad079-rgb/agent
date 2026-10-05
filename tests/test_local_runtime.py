"""Local runtime tests: sandbox, tools, boundary, lifecycle, sync, API.

These exercise the real local-runtime code paths — the filesystem sandbox
performs real path resolution, the tools perform real I/O, the boundary and
policy engines make real decisions. Only the LLM is never involved, because
none of this is model code.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arabic_agent.core.enums import (
    DataBoundary,
    DataSensitivity,
    OperatingMode,
    PlatformKind,
    RuntimeState,
    SyncKind,
    SyncStatus,
)
from arabic_agent.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from arabic_agent.local.boundary import DataBoundaryPolicy
from arabic_agent.local.capabilities import LocalCapabilityRegistry
from arabic_agent.local.command import CommandPolicy, ControlledCommandRunner
from arabic_agent.local.device import InMemoryDeviceRegistry, enroll_device
from arabic_agent.local.lifecycle import LifecycleStateMachine
from arabic_agent.local.modes import ModeManager
from arabic_agent.local.sandbox import FilesystemPolicy, FilesystemSandbox
from arabic_agent.local.sync import InMemorySyncQueue, SyncCoordinator, SyncTransport
from arabic_agent.permissions.base import (
    Permission,
    PolicyDecision,
    Principal,
    default_permission_risk,
)
from arabic_agent.permissions.policy import PolicyEngine
from arabic_agent.tools.base import ToolContext
from arabic_agent.tools.builtin.local import (
    CopyLocalFileTool,
    CreateLocalDirectoryTool,
    DeleteLocalFileTool,
    ListLocalDirectoryTool,
    MoveLocalFileTool,
    ReadLocalFileTool,
    RunLocalCommandTool,
    SearchLocalFilesTool,
    WriteLocalFileTool,
    sandbox_for,
)


# --- helpers ---------------------------------------------------------------
def _context(workspace: Path, *, policy: FilesystemPolicy | None = None) -> ToolContext:
    metadata: dict[str, object] = {}
    if policy is not None:
        metadata["filesystem_policy"] = policy
    return ToolContext(
        tenant_id="tnt_1",
        user_id="usr_1",
        run_id="run_1",
        workspace_dir=str(workspace),
        metadata=metadata,
    )


# --- Sandbox ---------------------------------------------------------------
def test_sandbox_requires_at_least_one_root():
    with pytest.raises(ValidationError):
        FilesystemSandbox(FilesystemPolicy(allowed_roots=()))


def test_sandbox_allows_path_inside_workspace(tmp_path):
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(tmp_path)))
    decision = sandbox.authorize_read(str(tmp_path / "note.txt"))
    assert decision.allowed


def test_sandbox_rejects_traversal(tmp_path):
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(tmp_path)))
    decision = sandbox.authorize_read("../outside.txt")
    assert not decision.allowed


def test_sandbox_rejects_absolute_outside_root(tmp_path):
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(tmp_path)))
    assert not sandbox.authorize_read("/etc/passwd").allowed


def test_sandbox_rejects_sensitive_names_even_under_allowed_root(tmp_path):
    root = tmp_path / "work"
    (root / ".ssh").mkdir(parents=True)
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(root)))
    assert not sandbox.authorize_read(str(root / ".ssh" / "id_rsa")).allowed


def test_sandbox_rejects_symlink_escape(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("top secret", encoding="utf-8")
    (root / "link").symlink_to(secret)
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(root)))
    assert not sandbox.authorize_read(str(root / "link")).allowed


def test_sandbox_deny_list_overrides_allow(tmp_path):
    root = tmp_path / "work"
    denied = root / "private"
    denied.mkdir(parents=True)
    sandbox = FilesystemSandbox(
        FilesystemPolicy(allowed_roots=(str(root),), denied_roots=(str(denied),))
    )
    assert sandbox.authorize_read(str(root / "public.txt")).allowed
    assert not sandbox.authorize_read(str(denied / "x.txt")).allowed


def test_sandbox_rejects_nul_byte_in_path(tmp_path):
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(tmp_path)))
    decision = sandbox.authorize_write("note\x00.txt")
    assert not decision.allowed
    assert decision.rule == "nul-byte"


def test_sandbox_rejects_malicious_filename_forms(tmp_path):
    sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(tmp_path)))
    for name in ("..", "../sibling", "", "   ", "\x00", "a/b\x00c"):
        assert not sandbox.authorize_write(name).allowed, name


# --- Local tools -----------------------------------------------------------
async def test_write_then_read_roundtrip(tmp_path):
    context = _context(tmp_path)
    write = await WriteLocalFileTool().run(
        {"path": "notes/todo.txt", "content": "مرحبا بالعالم"}, context
    )
    assert write.ok
    read = await ReadLocalFileTool().run({"path": "notes/todo.txt"}, context)
    assert read.ok
    assert read.data["content"] == "مرحبا بالعالم"


async def test_read_missing_file_raises_not_found(tmp_path):
    with pytest.raises(NotFoundError):
        await ReadLocalFileTool().run({"path": "nope.txt"}, _context(tmp_path))


async def test_list_directory_returns_entries(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    result = await ListLocalDirectoryTool().run({}, _context(tmp_path))
    names = {entry["name"] for entry in result.data["entries"]}
    assert names == {"a.txt", "sub"}


async def test_search_finds_matching_files(tmp_path):
    (tmp_path / "a.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "c.pdf").write_text("x", encoding="utf-8")
    result = await SearchLocalFilesTool().run({"pattern": "*.pdf"}, _context(tmp_path))
    found = {Path(m["path"]).name for m in result.data["matches"]}
    assert found == {"a.pdf", "c.pdf"}


async def test_search_does_not_descend_into_skipped_dirs(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "secret.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "real.pdf").write_text("x", encoding="utf-8")
    result = await SearchLocalFilesTool().run({"pattern": "*.pdf"}, _context(tmp_path))
    found = {Path(m["path"]).name for m in result.data["matches"]}
    assert found == {"real.pdf"}


async def test_write_outside_workspace_is_denied(tmp_path):
    with pytest.raises(PermissionDeniedError):
        await WriteLocalFileTool().run(
            {"path": "../escape.txt", "content": "x"}, _context(tmp_path)
        )


async def test_create_directory_tool(tmp_path):
    result = await CreateLocalDirectoryTool().run({"path": "new/dir"}, _context(tmp_path))
    assert result.ok
    assert (tmp_path / "new" / "dir").is_dir()


async def test_copy_refuses_overwrite_without_flag(tmp_path):
    (tmp_path / "src.txt").write_text("s", encoding="utf-8")
    (tmp_path / "dst.txt").write_text("d", encoding="utf-8")
    tool = CopyLocalFileTool()
    with pytest.raises(ConflictError):
        await tool.run({"source": "src.txt", "destination": "dst.txt"}, _context(tmp_path))
    ok = await tool.run(
        {"source": "src.txt", "destination": "dst.txt", "overwrite": True},
        _context(tmp_path),
    )
    assert ok.ok
    assert (tmp_path / "dst.txt").read_text(encoding="utf-8") == "s"


async def test_move_file_tool(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    result = await MoveLocalFileTool().run(
        {"source": "a.txt", "destination": "b.txt"}, _context(tmp_path)
    )
    assert result.ok
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "a"


async def test_delete_file_tool(tmp_path):
    target = tmp_path / "gone.txt"
    target.write_text("x", encoding="utf-8")
    result = await DeleteLocalFileTool().run({"path": "gone.txt"}, _context(tmp_path))
    assert result.ok
    assert not target.exists()


async def test_delete_refuses_directory(tmp_path):
    (tmp_path / "dir").mkdir()
    with pytest.raises(ValidationError):
        await DeleteLocalFileTool().run({"path": "dir"}, _context(tmp_path))


async def test_command_tool_without_policy_is_denied(tmp_path):
    with pytest.raises(PermissionDeniedError):
        await RunLocalCommandTool().run({"command": "echo hi"}, _context(tmp_path))


async def test_command_tool_runs_allowlisted_command(tmp_path):
    policy = CommandPolicy(allowlist=frozenset({"echo"}), allowed_cwd_roots=(str(tmp_path),))
    context = _context(tmp_path)
    context = context.model_copy(
        update={"metadata": {**context.metadata, "command_policy": policy}}
    )
    result = await RunLocalCommandTool().run({"command": "echo hello"}, context)
    assert result.ok
    assert "hello" in result.data["stdout"]


async def test_command_tool_refuses_non_allowlisted(tmp_path):
    policy = CommandPolicy(allowlist=frozenset({"echo"}), allowed_cwd_roots=(str(tmp_path),))
    context = _context(tmp_path).model_copy(update={"metadata": {"command_policy": policy}})
    with pytest.raises(PermissionDeniedError):
        await RunLocalCommandTool().run({"command": "rm -rf /"}, context)


# --- Command runner --------------------------------------------------------
def test_command_runner_rejects_shell_metacharacters():
    runner = ControlledCommandRunner(
        CommandPolicy(allowlist=frozenset({"echo"}), allowed_cwd_roots=("/tmp",))
    )
    # No shell: the metacharacter stays inside an argument rather than
    # splitting into a second command.
    argv = runner.parse("echo hello; rm -rf /")
    assert argv == ["echo", "hello;", "rm", "-rf", "/"]


async def test_command_runner_enforces_timeout(tmp_path):
    policy = CommandPolicy(
        allowlist=frozenset({"sleep"}),
        allowed_cwd_roots=(str(tmp_path),),
        timeout_seconds=0.2,
    )
    runner = ControlledCommandRunner(policy)
    from arabic_agent.core.errors import TimeoutError_

    with pytest.raises(TimeoutError_):
        await runner.run("sleep 5", cwd=str(tmp_path))


async def test_command_runner_never_passes_secrets(tmp_path):
    policy = CommandPolicy(allowlist=frozenset({"env"}), allowed_cwd_roots=(str(tmp_path),))
    runner = ControlledCommandRunner(policy)
    os.environ["AAP_SUPER_SECRET"] = "leak-me"
    try:
        result = await runner.run("env", cwd=str(tmp_path))
    finally:
        os.environ.pop("AAP_SUPER_SECRET", None)
    assert "leak-me" not in result.stdout


# --- Boundary --------------------------------------------------------------
def test_boundary_never_routes_secret_remotely():
    policy = DataBoundaryPolicy(OperatingMode.ONLINE)
    decision = policy.evaluate(boundary=DataBoundary.REMOTE, sensitivity=DataSensitivity.SECRET)
    assert not decision.allowed


def test_boundary_confidential_remote_denied_by_default():
    policy = DataBoundaryPolicy(OperatingMode.ONLINE)
    decision = policy.evaluate(
        boundary=DataBoundary.REMOTE, sensitivity=DataSensitivity.CONFIDENTIAL
    )
    assert not decision.allowed


def test_boundary_offline_forces_local():
    policy = DataBoundaryPolicy(OperatingMode.OFFLINE)
    decision = policy.evaluate(boundary=DataBoundary.EITHER, sensitivity=DataSensitivity.INTERNAL)
    assert decision.allowed
    assert decision.routed_to is DataBoundary.LOCAL


def test_boundary_local_always_allowed():
    policy = DataBoundaryPolicy(OperatingMode.OFFLINE)
    decision = policy.evaluate(boundary=DataBoundary.LOCAL, sensitivity=DataSensitivity.SECRET)
    assert decision.allowed


# --- Lifecycle -------------------------------------------------------------
def test_lifecycle_happy_path():
    machine = LifecycleStateMachine()
    assert machine.state is RuntimeState.STOPPED
    machine.transition(RuntimeState.STARTING)
    machine.transition(RuntimeState.READY)
    assert machine.accepts_tools
    machine.transition(RuntimeState.RUNNING)
    assert machine.accepts_tools
    machine.transition(RuntimeState.PAUSED)
    assert not machine.accepts_tools
    machine.transition(RuntimeState.RUNNING)
    machine.transition(RuntimeState.STOPPING)
    machine.transition(RuntimeState.STOPPED)


def test_lifecycle_rejects_invalid_transition():
    machine = LifecycleStateMachine()
    from arabic_agent.core.errors import ConflictError as CE

    with pytest.raises(CE):
        machine.transition(RuntimeState.READY)


# --- Modes -----------------------------------------------------------------
def test_mode_manager_online_flags():
    modes = ModeManager(OperatingMode.OFFLINE)
    assert modes.offline
    assert not modes.online
    assert not modes.may_use_cloud()
    modes.set_mode(OperatingMode.HYBRID)
    assert modes.online
    assert modes.may_use_cloud()


# --- Devices ---------------------------------------------------------------
def test_enroll_device_has_no_hardware_id():
    device = enroll_device(
        tenant_id="tnt_1",
        user_id="usr_1",
        label="laptop",
        platform=PlatformKind.LINUX,
        instance_fingerprint="fingerprint-1",
    )
    assert device.id.startswith("dev_")
    assert device.label == "laptop"
    assert device.instance_fingerprint == "fingerprint-1"
    assert "serial" not in device.model_dump()
    assert "mac" not in device.model_dump()


async def test_device_registry_roundtrip():
    registry = InMemoryDeviceRegistry()
    device = enroll_device(
        tenant_id="tnt_1",
        user_id="usr_1",
        label="pc",
        platform=PlatformKind.LINUX,
        instance_fingerprint="fp",
    )
    await registry.enroll(device)
    fetched = await registry.get(device.id, tenant_id="tnt_1")
    assert fetched.id == device.id
    from arabic_agent.core.errors import NotFoundError as NF

    with pytest.raises(NF):
        await registry.get(device.id, tenant_id="other")


# --- Sync ------------------------------------------------------------------
class _RecordingTransport(SyncTransport):
    def __init__(self) -> None:
        self.uploaded: list[str] = []

    async def upload(self, actions):  # type: ignore[no-untyped-def]
        self.uploaded = [a.id for a in actions]
        return list(self.uploaded)


async def test_sync_enqueue_and_flush_with_transport():
    queue = InMemorySyncQueue()
    transport = _RecordingTransport()
    coordinator = SyncCoordinator(
        queue, boundary=DataBoundaryPolicy(OperatingMode.HYBRID), transport=transport
    )
    action = await coordinator.enqueue(
        tenant_id="tnt_1",
        user_id="usr_1",
        device_id="dev_1",
        kind=SyncKind.WORKFLOW_STATUS,
        payload={"run_id": "run_1", "status": "succeeded"},
    )
    assert action.status is SyncStatus.PENDING
    uploaded = await coordinator.flush(tenant_id="tnt_1")
    assert uploaded == [action.id]
    stored = await queue.get(action.id, tenant_id="tnt_1")
    assert stored.status is SyncStatus.SYNCED


async def test_sync_flush_without_transport_is_noop():
    queue = InMemorySyncQueue()
    coordinator = SyncCoordinator(queue, boundary=DataBoundaryPolicy(OperatingMode.HYBRID))
    action = await coordinator.enqueue(
        tenant_id="tnt_1",
        user_id="usr_1",
        device_id="dev_1",
        kind=SyncKind.DEVICE_STATUS,
        payload={"state": "ready"},
    )
    assert await coordinator.flush(tenant_id="tnt_1") == []
    stored = await queue.get(action.id, tenant_id="tnt_1")
    assert stored.status is SyncStatus.PENDING


async def test_sync_rejects_confidential_payload_offline():
    queue = InMemorySyncQueue()
    coordinator = SyncCoordinator(queue, boundary=DataBoundaryPolicy(OperatingMode.OFFLINE))
    action = await coordinator.enqueue(
        tenant_id="tnt_1",
        user_id="usr_1",
        device_id="dev_1",
        kind=SyncKind.SELECTED_MEMORY,
        payload={"content": "sensitive"},
    )
    assert action.status is SyncStatus.REJECTED


async def test_sync_redacts_secret_like_payload_keys():
    queue = InMemorySyncQueue()
    coordinator = SyncCoordinator(queue, boundary=DataBoundaryPolicy(OperatingMode.HYBRID))
    action = await coordinator.enqueue(
        tenant_id="tnt_1",
        user_id="usr_1",
        device_id="dev_1",
        kind=SyncKind.TASK_STATUS,
        payload={"api_key": "sk-secret", "task": "ok"},
    )
    assert action.payload["api_key"] != "sk-secret"


# --- Capabilities ----------------------------------------------------------
def test_capability_registry_marks_only_safe_caps_implemented():
    registry = LocalCapabilityRegistry()
    implemented = {c.id for c in registry.implemented()}
    assert implemented == {
        "files.read",
        "files.list",
        "files.search",
        "files.write",
        "files.copy",
        "files.move",
        "files.mkdir",
        "files.delete",
        "command.execute",
    }
    assert "input.control" not in implemented


def test_capability_availability_respects_platform_and_network():
    registry = LocalCapabilityRegistry()
    available = registry.available(platform=PlatformKind.LINUX, online=False)
    ids = {c.id for c in available}
    assert "files.read" in ids
    # A network-requiring capability must not be available offline.
    for cap in registry.all():
        if cap.requires_network:
            assert cap.id not in ids


def test_sandbox_for_uses_metadata_policy(tmp_path):
    extra = tmp_path / "extra"
    extra.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    policy = FilesystemPolicy(allowed_roots=(str(extra),))
    context = _context(workspace, policy=policy)
    sandbox = sandbox_for(context)
    # Both the workspace and the operator-approved extra root are allowed.
    assert sandbox.authorize_read(str(extra / "x.txt")).allowed
    assert sandbox.authorize_read(str(workspace / "y.txt")).allowed


def test_sandbox_provider_merges_extra_roots(tmp_path):
    from arabic_agent.local.sandbox_provider import SandboxProvider

    workspace = tmp_path / "ws"
    workspace.mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    context = _context(workspace)
    sandbox = SandboxProvider(extra_roots=(str(extra),)).for_context(context)
    assert sandbox.authorize_read(str(workspace / "a.txt")).allowed
    assert sandbox.authorize_read(str(extra / "b.txt")).allowed
    assert not sandbox.authorize_read(str(tmp_path / "elsewhere.txt")).allowed
    # Relative paths anchor to the workspace, not the process CWD.
    assert sandbox.authorize_read("rel.txt").allowed


def test_sandbox_provider_honours_context_deny_list(tmp_path):
    from arabic_agent.local.sandbox_provider import SandboxProvider

    workspace = tmp_path / "ws"
    workspace.mkdir()
    denied = workspace / "private"
    denied.mkdir()
    policy = FilesystemPolicy(allowed_roots=(str(workspace),), denied_roots=(str(denied),))
    context = _context(workspace, policy=policy)
    sandbox = SandboxProvider().for_context(context)
    assert not sandbox.authorize_read(str(denied / "x.txt")).allowed


def test_sandbox_provider_requires_a_root(tmp_path):
    from arabic_agent.local.sandbox_provider import SandboxProvider

    context = ToolContext(tenant_id="t", user_id="u", run_id="r", workspace_dir="")
    with pytest.raises(ValidationError):
        SandboxProvider().for_context(context)


# --- Policy integration (local permissions) --------------------------------
def _evaluate(principal: Principal, tool: str, permission: Permission) -> PolicyDecision:
    return PolicyEngine().evaluate(
        principal,
        tool_name=tool,
        permission=permission,
        risk=default_permission_risk(permission),
    )


def _principal(**kwargs: object) -> Principal:
    defaults: dict = {"tenant_id": "tnt_1", "user_id": "usr_1"}
    defaults.update(kwargs)
    return Principal(**defaults)


def test_local_read_permission_allowed_by_default():
    decision = _evaluate(_principal(), "read_local_file", Permission.FILES_READ)
    assert decision.allowed


def test_local_write_requires_approval_by_default():
    decision = _evaluate(_principal(), "write_local_file", Permission.FILES_WRITE)
    assert decision.requires_approval


def test_local_delete_denied_by_default():
    decision = _evaluate(_principal(), "delete_local_file", Permission.FILES_DELETE)
    assert decision.denied


def test_local_command_requires_approval_by_default():
    decision = _evaluate(_principal(), "run_local_command", Permission.RUN_COMMAND)
    assert decision.requires_approval


def test_local_input_control_denied_by_default():
    decision = _evaluate(_principal(), "control_input", Permission.INPUT_CONTROL)
    assert decision.denied
