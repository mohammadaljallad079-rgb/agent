"""Local control API.

The desktop UI must never touch the operating system directly. It talks to the
local runtime through this control surface, which is authenticated and enforces
the same policy as every other path:

    Desktop UI → authenticated local API → Local Runtime → Permission Layer → Tool

The API is a thin *read/control* surface: it reports status, lists
capabilities and pending work, and drives the runtime lifecycle and mode. It
never executes a tool directly — tool execution goes through the existing
:class:`~arabic_agent.agent.executor.ToolExecutor`, so permissions, approvals
and audit are unchanged.

Authentication is a shared secret compared in constant time. The surface refuses
every request when no secret is configured unless the operator has explicitly
disabled auth in a non-production environment.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

from ..container import Platform
from ..core.config import Settings
from ..core.enums import OperatingMode
from ..core.errors import PermissionDeniedError
from .runtime import LocalRuntime


@dataclass(frozen=True)
class ControlSnapshot:
    """Everything the local API can report in one read."""

    runtime: dict[str, object]
    capabilities: list[dict[str, object]]
    tools: list[str]
    pending_approvals: int
    active_runs: int
    pending_sync: int
    local_model: dict[str, object]


class LocalControlAPI:
    """Authenticated control/status service for the local runtime."""

    def __init__(
        self,
        *,
        runtime: LocalRuntime,
        platform: Platform,
        settings: Settings,
    ) -> None:
        self._runtime = runtime
        self._platform = platform
        self._settings = settings
        self._token = settings.local_control_token.get_secret_value()

    # -- Authentication --------------------------------------------------
    @property
    def auth_configured(self) -> bool:
        return bool(self._token)

    def authorize(self, token: str | None) -> None:
        """Validate a control token, or raise :class:`PermissionDeniedError`.

        Fails closed: if no token is configured, access is refused unless the
        operator has explicitly turned auth off in a non-production environment.
        """
        if self.auth_configured:
            if token is None or not secrets.compare_digest(token, self._token):
                raise PermissionDeniedError("invalid local control token")
            return
        if not self._settings.require_auth and not self._settings.is_production:
            return
        raise PermissionDeniedError("local control API has no token configured")

    # -- Reads -----------------------------------------------------------
    async def snapshot(self, *, tenant_id: str) -> ControlSnapshot:
        status = self._runtime.status()
        capabilities: list[dict[str, object]] = [
            {
                "id": c.id,
                "name": c.name,
                "description": c.description,
                "tool_name": c.tool_name,
                "risk": c.risk.value,
                "permissions": [p.value for p in c.permissions],
                "platforms": [p.value for p in c.platforms],
                "available_offline": c.available_offline,
                "requires_network": c.requires_network,
                "data_boundary": c.data_boundary.value,
                "requires_approval": c.requires_approval,
                "implemented": c.implemented,
                "audit_events": list(c.audit_events),
            }
            for c in self._runtime.capabilities.all()
        ]
        pending_approvals = await self._platform.approvals.list_pending(tenant_id=tenant_id)
        from ..core.enums import RunStatus

        active = await self._platform.runs.list_runs(
            tenant_id=tenant_id, status=RunStatus.RUNNING, limit=100
        )
        pending_sync = await self._platform.sync_queue.list_pending(tenant_id=tenant_id)
        return ControlSnapshot(
            runtime=status.model_dump(mode="json"),
            capabilities=capabilities,
            tools=self._platform.tools.names(),
            pending_approvals=len(pending_approvals),
            active_runs=len(active),
            pending_sync=len(pending_sync),
            local_model={
                "available": status.local_model_available,
                "detail": status.local_model_detail,
                "provider": self._settings.local_model_name
                if status.local_model_available
                else None,
            },
        )

    async def pending_approvals(self, *, tenant_id: str) -> list[dict[str, object]]:
        approvals = await self._platform.approvals.list_pending(tenant_id=tenant_id)
        return [
            {
                "id": a.id,
                "tool_name": a.tool_name,
                "permission": a.permission.value,
                "summary": a.summary,
                "run_id": a.run_id,
                "expires_at": a.expires_at.isoformat(),
            }
            for a in approvals
        ]

    async def pending_sync(self, *, tenant_id: str) -> list[dict[str, object]]:
        actions = await self._platform.sync_queue.list_all(tenant_id=tenant_id)
        return [
            {
                "id": a.id,
                "kind": a.kind.value,
                "status": a.status.value,
                "created_at": a.created_at.isoformat(),
            }
            for a in actions
        ]

    # -- Lifecycle control ----------------------------------------------
    async def set_mode(self, mode: OperatingMode) -> dict[str, object]:
        status = await self._runtime.set_mode(mode)
        return status.model_dump(mode="json")

    async def lifecycle(self, action: str) -> dict[str, object]:
        """Drive the runtime lifecycle: start/run/pause/resume/stop."""
        if action == "start":
            status = await self._runtime.start()
        elif action == "run":
            status = await self._runtime.run()
        elif action == "pause":
            status = await self._runtime.pause()
        elif action == "resume":
            status = await self._runtime.resume()
        elif action == "stop":
            status = await self._runtime.stop()
        else:
            from ..core.errors import ValidationError

            raise ValidationError(
                f"unknown lifecycle action: {action}",
                details={"allowed": ["start", "run", "pause", "resume", "stop"]},
            )
        return status.model_dump(mode="json")

    async def flush_sync(self, *, tenant_id: str) -> list[str]:
        return await self._runtime.flush_sync(tenant_id=tenant_id)
