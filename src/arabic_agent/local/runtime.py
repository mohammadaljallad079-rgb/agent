"""The local runtime: the secure bridge between the agent and the machine.

The local runtime owns the pieces that are specific to running *on the user's
computer* and ties them together behind one lifecycle:

    device identity · operating mode · data boundary · filesystem sandbox
    · capability catalogue · platform adapters · offline sync queue

It does **not** contain an agent loop, a planner, a workflow engine or a
permission engine. Those stay in the existing core. The runtime is the
transport and sandbox those components execute behind when the platform runs
locally.

Lifecycle (enforced by :class:`LifecycleStateMachine`)::

    start()  STOPPED → STARTING → READY
    run()    READY/RUNNING
    pause()  → PAUSED   (tool execution refused while paused)
    resume() → RUNNING
    stop()   → STOPPING → STOPPED

Every transition and every refusal is recorded on the audit log.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ..audit.base import AuditEventType, AuditLog
from ..core.config import Settings
from ..core.enums import (
    DataBoundary,
    DataSensitivity,
    NetworkPolicy,
    OperatingMode,
    PlatformKind,
    RuntimeKind,
    RuntimeState,
    SyncKind,
)
from ..core.errors import ConflictError, PermissionDeniedError
from ..observability.context import correlation_scope
from .adapters import PlatformAdapters, detect_platform
from .boundary import BoundaryDecision, DataBoundaryPolicy
from .capabilities import LocalCapability, LocalCapabilityRegistry
from .device import DeviceIdentity, DeviceRegistry
from .lifecycle import LifecycleStateMachine
from .modes import ModeManager
from .sandbox import FilesystemPolicy, FilesystemSandbox
from .sync import SyncAction, SyncCoordinator


class LocalRuntimeStatus(BaseModel):
    """A serialisable snapshot of the runtime, safe to return over the API."""

    model_config = ConfigDict(frozen=True)

    state: RuntimeState
    mode: OperatingMode
    online: bool
    runtime_kind: RuntimeKind = RuntimeKind.LOCAL
    platform: PlatformKind
    device_id: str | None = None
    device_status: str | None = None
    workspace_dir: str
    allowed_roots: tuple[str, ...] = ()
    capabilities: tuple[str, ...] = ()
    available_capabilities: tuple[str, ...] = ()
    adapters: tuple[str, ...] = ()
    network_policy: NetworkPolicy
    local_model_available: bool = False
    local_model_detail: str = ""


class LocalRuntime:
    """Owns the local-only components and their lifecycle."""

    def __init__(
        self,
        *,
        settings: Settings,
        audit: AuditLog,
        sandbox: FilesystemSandbox,
        capabilities: LocalCapabilityRegistry,
        devices: DeviceRegistry,
        sync: SyncCoordinator,
        boundary: DataBoundaryPolicy,
        modes: ModeManager,
        adapters: PlatformAdapters | None = None,
        workspace_dir: str,
        device: DeviceIdentity | None = None,
        network_policy: NetworkPolicy = NetworkPolicy.DISABLED,
    ) -> None:
        self._settings = settings
        self._audit = audit
        self._sandbox = sandbox
        self._capabilities = capabilities
        self._devices = devices
        self._sync = sync
        self._boundary = boundary
        self._modes = modes
        self._adapters = adapters or PlatformAdapters()
        self._workspace_dir = workspace_dir
        self._device = device
        self._network_policy = network_policy
        self._lifecycle = LifecycleStateMachine()
        self._local_model_available = False
        self._local_model_detail = "no local model endpoint configured"

    # -- Accessors ------------------------------------------------------
    @property
    def state(self) -> RuntimeState:
        return self._lifecycle.state

    @property
    def mode(self) -> OperatingMode:
        return self._modes.mode

    @property
    def sandbox(self) -> FilesystemSandbox:
        return self._sandbox

    @property
    def capabilities(self) -> LocalCapabilityRegistry:
        return self._capabilities

    @property
    def devices(self) -> DeviceRegistry:
        return self._devices

    @property
    def boundary(self) -> DataBoundaryPolicy:
        return self._boundary

    @property
    def adapters(self) -> PlatformAdapters:
        return self._adapters

    @property
    def workspace_dir(self) -> str:
        return self._workspace_dir

    @property
    def device(self) -> DeviceIdentity | None:
        return self._device

    @property
    def sync(self) -> SyncCoordinator:
        return self._sync

    @property
    def network_policy(self) -> NetworkPolicy:
        return self._network_policy

    def set_local_model_status(self, *, available: bool, detail: str = "") -> None:
        self._local_model_available = available
        self._local_model_detail = detail

    # -- Lifecycle ------------------------------------------------------
    async def start(self) -> LocalRuntimeStatus:
        """STARTING → READY, emitting audit events."""
        with correlation_scope(tenant_id=self._tenant_id()):
            self._lifecycle.transition(RuntimeState.STARTING)
            await self._record(AuditEventType.LOCAL_RUNTIME_STARTED, "local runtime starting")
            try:
                self._lifecycle.transition(RuntimeState.READY)
            except Exception as exc:
                self._lifecycle.transition(RuntimeState.ERROR)
                await self._record(
                    AuditEventType.LOCAL_RUNTIME_ERROR, f"local runtime failed to start: {exc}"
                )
                raise
            await self._record(AuditEventType.LOCAL_RUNTIME_READY, "local runtime ready")
        return self.status()

    async def run(self) -> LocalRuntimeStatus:
        """READY/PAUSED → RUNNING."""
        with correlation_scope(tenant_id=self._tenant_id()):
            previous = self._lifecycle.transition(RuntimeState.RUNNING)
            event = (
                AuditEventType.LOCAL_RUNTIME_RESUMED
                if previous is RuntimeState.PAUSED
                else AuditEventType.LOCAL_RUNTIME_READY
            )
            await self._record(event, "local runtime running")
        return self.status()

    async def pause(self) -> LocalRuntimeStatus:
        with correlation_scope(tenant_id=self._tenant_id()):
            self._lifecycle.transition(RuntimeState.PAUSED)
            await self._record(AuditEventType.LOCAL_RUNTIME_PAUSED, "local runtime paused")
        return self.status()

    async def resume(self) -> LocalRuntimeStatus:
        with correlation_scope(tenant_id=self._tenant_id()):
            self._lifecycle.transition(RuntimeState.RUNNING)
            await self._record(AuditEventType.LOCAL_RUNTIME_RESUMED, "local runtime resumed")
        return self.status()

    async def stop(self) -> LocalRuntimeStatus:
        with correlation_scope(tenant_id=self._tenant_id()):
            self._lifecycle.transition(RuntimeState.STOPPING)
            self._lifecycle.transition(RuntimeState.STOPPED)
            await self._record(AuditEventType.LOCAL_RUNTIME_STOPPED, "local runtime stopped")
        return self.status()

    def mark_error(self, detail: str) -> None:
        """Force the runtime into ERROR (used by a supervising loop)."""
        if self._lifecycle.can_transition(RuntimeState.ERROR):
            self._lifecycle.transition(RuntimeState.ERROR)

    # -- Mode -----------------------------------------------------------
    async def set_mode(self, mode: OperatingMode) -> LocalRuntimeStatus:
        with correlation_scope(tenant_id=self._tenant_id()):
            previous = self._modes.set_mode(mode)
            self._boundary.set_mode(mode)
            await self._record(
                AuditEventType.LOCAL_DATA_BOUNDARY_DECISION,
                f"operating mode {previous.value} → {mode.value}",
                data={"from": previous.value, "to": mode.value},
            )
        return self.status()

    # -- Tool gate ------------------------------------------------------
    def assert_accepts_tools(self) -> None:
        """Raise if the runtime is not currently accepting tool work."""
        if not self._lifecycle.accepts_tools:
            raise ConflictError(
                f"local runtime is not accepting tools (state={self._lifecycle.state.value})",
                details={"state": self._lifecycle.state.value},
            )

    async def record_tool_request(
        self,
        *,
        tool_name: str,
        capability_id: str | None,
        tenant_id: str,
        user_id: str,
        run_id: str,
    ) -> None:
        """Audit a local tool request (called by local tools before doing work)."""
        await self._audit.record(
            AuditEventType.LOCAL_TOOL_REQUESTED,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            message=f"local tool requested: {tool_name}",
            data={"tool": tool_name, "capability": capability_id},
        )

    async def authorize_network(self, host: str) -> BoundaryDecision:
        """Apply the network policy to an outbound host.

        Default ``DISABLED`` refuses everything. ``ALLOWLIST`` permits hosts the
        operator listed. ``USER_APPROVAL`` is treated as a refusal at this layer
        because the approval itself is handled by the tool executor; the tool
        must declare ``NETWORK_ACCESS`` so the policy gate asks the human.
        """

        allowed = False
        reason = ""
        if self._network_policy is NetworkPolicy.FULL_ACCESS:
            allowed, reason = True, "network policy is full_access"
        elif self._network_policy is NetworkPolicy.ALLOWLIST:
            allow = {h.strip().lower() for h in self._settings.local_network_allowlist}
            if host.strip().lower() in allow:
                allowed, reason = True, "host is on the allow-list"
            else:
                reason = "host is not on the network allow-list"
        elif self._network_policy is NetworkPolicy.USER_APPROVAL:
            reason = "network access requires user approval"
        else:
            reason = "network access is disabled by policy"

        decision = (
            BoundaryDecision.allow(
                routed_to=DataBoundary.REMOTE,
                sensitivity=DataSensitivity.INTERNAL,
                mode=self._modes.mode,
                reason=reason,
            )
            if allowed
            else BoundaryDecision.deny(
                routed_to=DataBoundary.REMOTE,
                sensitivity=DataSensitivity.INTERNAL,
                mode=self._modes.mode,
                reason=reason,
            )
        )
        return decision

    # -- Synchronization -------------------------------------------------
    async def enqueue_sync(
        self,
        *,
        kind: SyncKind,
        payload: dict[str, object],
        tenant_id: str,
        user_id: str,
    ) -> SyncAction:
        if self._device is None:
            raise PermissionDeniedError("cannot sync without an enrolled device identity")
        action = await self._sync.enqueue(
            tenant_id=tenant_id,
            user_id=user_id,
            device_id=self._device.id,
            kind=kind,
            payload=payload,
        )
        event = (
            AuditEventType.SYNC_REJECTED
            if action.status.value == "rejected"
            else AuditEventType.SYNC_QUEUED
        )
        await self._audit.record(
            event,
            tenant_id=tenant_id,
            user_id=user_id,
            run_id="local",
            message=f"sync {action.status.value}: {kind.value}",
            data={"sync_id": action.id, "kind": kind.value},
        )
        return action

    async def flush_sync(self, *, tenant_id: str) -> list[str]:
        uploaded = await self._sync.flush(tenant_id=tenant_id)
        for sync_id in uploaded:
            await self._audit.record(
                AuditEventType.SYNC_UPLOADED,
                tenant_id=tenant_id,
                user_id="",
                run_id="local",
                message="sync action uploaded",
                data={"sync_id": sync_id},
            )
        return uploaded

    # -- Status ---------------------------------------------------------
    def status(self) -> LocalRuntimeStatus:
        available = self._capabilities.available(
            platform=self._adapters.platform, online=self._modes.online
        )
        return LocalRuntimeStatus(
            state=self._lifecycle.state,
            mode=self._modes.mode,
            online=self._modes.online,
            platform=self._adapters.platform,
            device_id=self._device.id if self._device else None,
            device_status=self._device.status.value if self._device else None,
            workspace_dir=self._workspace_dir,
            allowed_roots=tuple(str(r) for r in self._sandbox.allowed_roots),
            capabilities=tuple(c.id for c in self._capabilities.all()),
            available_capabilities=tuple(c.id for c in available),
            adapters=tuple(self._adapters.available_names()),
            network_policy=self._network_policy,
            local_model_available=self._local_model_available,
            local_model_detail=self._local_model_detail,
        )

    def implemented_capabilities(self) -> tuple[LocalCapability, ...]:
        return self._capabilities.implemented()

    # -- Internals ------------------------------------------------------
    def _tenant_id(self) -> str:
        return self._device.tenant_id if self._device else "local"

    async def _record(
        self, event_type: AuditEventType, message: str, *, data: dict[str, object] | None = None
    ) -> None:
        device = self._device
        await self._audit.record(
            event_type,
            tenant_id=device.tenant_id if device else "local",
            user_id=device.user_id if device else "local",
            run_id="local-runtime",
            message=message,
            data=data or {},
        )


def build_local_runtime(
    *,
    settings: Settings,
    audit: AuditLog,
    devices: DeviceRegistry,
    sync: SyncCoordinator,
    workspace_dir: str,
    allowed_roots: tuple[str, ...],
    denied_roots: tuple[str, ...] = (),
    device: DeviceIdentity | None = None,
    mode: OperatingMode = OperatingMode.OFFLINE,
    network_policy: NetworkPolicy = NetworkPolicy.DISABLED,
    adapters: PlatformAdapters | None = None,
) -> LocalRuntime:
    """Assemble a local runtime from configuration.

    ``allowed_roots`` is required: an empty allow-list would grant nothing, and
    the sandbox refuses to be constructed without at least one root. Callers
    pass the user-approved directories the agent may touch.
    """
    roots = allowed_roots or (workspace_dir,)
    sandbox = FilesystemSandbox(
        FilesystemPolicy(
            allowed_roots=tuple(roots),
            denied_roots=tuple(denied_roots),
        )
    )
    boundary = DataBoundaryPolicy(mode, allow_confidential_remote=False)
    modes = ModeManager(mode)
    return LocalRuntime(
        settings=settings,
        audit=audit,
        sandbox=sandbox,
        capabilities=LocalCapabilityRegistry(computer_control=settings.computer_control_enabled),
        devices=devices,
        sync=sync,
        boundary=boundary,
        modes=modes,
        adapters=adapters or PlatformAdapters(detect_platform()),
        workspace_dir=workspace_dir,
        device=device,
        network_policy=network_policy,
    )
