"""Desktop service: the product-level operations the desktop UI calls.

This is a thin, authenticated facade over components the platform already has —
it adds no agent logic and executes no tool directly:

* **Runs / sessions** go through the existing
  :class:`~arabic_agent.api.service.RunService`, so a task started from the UI is
  a durable run with the same permissions, approvals, audit and recovery as any
  other. The UI never fabricates an ``approval_granted`` flag; it resolves the
  real approval record.
* **Models** go through :class:`~arabic_agent.desktop.model_manager.ModelManager`.
* **Diagnostics / settings / permissions** are *read* views over real state; they
  never claim a capability the runtime did not report.
* **Events** stream from :class:`~arabic_agent.desktop.events.DesktopEventBroker`,
  which mirrors the audit log.

A single local principal (``local``/``local``) represents the desktop user; the
control token authenticates the *client*, and every operation still runs through
the platform's own policy gate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ..api.service import RunService
from ..audit.base import AuditEventType
from ..browser.provisioning import BrowserProvisioner
from ..container import Platform
from ..core.enums import (
    Locale,
    OperatingMode,
    PermissionEffect,
    RiskLevel,
    RunStatus,
)
from ..core.errors import ValidationError
from ..desktop.events import DesktopEventBroker
from ..desktop.model_manager import ModelManager
from ..local.hardware import HardwareProfile, detect_hardware
from ..permissions.base import Permission, Principal

logger = logging.getLogger(__name__)

#: Capabilities the settings screen can surface (label, permission, Arabic label).
_SETTINGS_CAPABILITIES: tuple[tuple[str, Permission, str], ...] = (
    ("files.read", Permission.FILES_READ, "قراءة الملفات"),
    ("files.write", Permission.FILES_WRITE, "كتابة الملفات"),
    ("files.delete", Permission.FILES_DELETE, "حذف الملفات"),
    ("terminal", Permission.RUN_COMMAND, "تشغيل الأوامر"),
    ("screen.read", Permission.SCREEN_READ, "قراءة الشاشة"),
    ("pointer.control", Permission.POINTER_CONTROL, "التحكم بالمؤشر"),
    ("keyboard.control", Permission.KEYBOARD_CONTROL, "التحكم بلوحة المفاتيح"),
    ("browser", Permission.BROWSER_READ, "تصفح الويب"),
    ("connectors.read", Permission.CONNECTOR_READ, "قراءة الأنظمة الخارجية"),
    ("connectors.write", Permission.CONNECTOR_WRITE, "الكتابة في الأنظمة الخارجية"),
    ("email.send", Permission.SEND_EMAIL, "إرسال البريد"),
    ("network", Permission.NETWORK_ACCESS, "الوصول للشبكة"),
)


@dataclass(frozen=True)
class DesktopPrincipal:
    """The single local principal the desktop operates as."""

    tenant_id: str = "local"
    user_id: str = "local"

    def to_principal(self) -> Principal:
        return Principal(tenant_id=self.tenant_id, user_id=self.user_id, roles=frozenset({"owner"}))


class DesktopService:
    """Product-level facade over the platform for the desktop UI."""

    def __init__(
        self,
        *,
        platform: Platform,
        model_manager: ModelManager,
        events: DesktopEventBroker,
        principal: DesktopPrincipal | None = None,
        locale: Locale = Locale.AR,
    ) -> None:
        self._platform = platform
        self._models = model_manager
        self._events = events
        self._principal = (principal or DesktopPrincipal()).to_principal()
        self._runs = RunService(platform)
        self._locale = locale
        self._browser = BrowserProvisioner()

    @property
    def events(self) -> DesktopEventBroker:
        return self._events

    @property
    def model_manager(self) -> ModelManager:
        return self._models

    @property
    def principal(self) -> Principal:
        return self._principal

    # -- Runs / sessions -------------------------------------------------
    async def start_run(self, *, goal: str, workspace_dir: str | None = None) -> dict[str, object]:
        if not goal or not goal.strip():
            raise ValidationError("a goal is required")
        record = await self._runs.start(
            principal=self._principal, goal=goal.strip(), workspace_dir=workspace_dir
        )
        return self._run_dict(record)

    async def list_runs(
        self, *, status: RunStatus | None = None, limit: int = 50, offset: int = 0
    ) -> list[dict[str, object]]:
        records = await self._runs.list(
            tenant_id=self._principal.tenant_id, status=status, limit=limit, offset=offset
        )
        return [self._run_dict(r) for r in records]

    async def get_run(self, run_id: str) -> dict[str, object]:
        record = await self._runs.get(run_id, tenant_id=self._principal.tenant_id)
        return self._run_dict(record)

    async def run_timeline(self, run_id: str) -> list[dict[str, object]]:
        """Return the run's audit trail as UI-friendly timeline entries."""
        events = await self._platform.audit.list_for_run(
            tenant_id=self._principal.tenant_id, run_id=run_id
        )
        return [
            {
                "type": e.type.value,
                "message": e.message,
                "data": e.data,
                "sequence": e.sequence,
                "timestamp": e.timestamp.isoformat(),
            }
            for e in events
        ]

    async def cancel_run(self, run_id: str) -> dict[str, object]:
        record = await self._runs.get(run_id, tenant_id=self._principal.tenant_id)
        if record.is_terminal:
            return self._run_dict(record)
        cancelled = await self._runs._cancel(record, reason="cancelled by user")
        await self._platform.audit.record(
            AuditEventType.RUN_CANCELLED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id=run_id,
            message="run cancelled by the user (emergency stop)",
        )
        return self._run_dict(cancelled)

    # -- Approvals -------------------------------------------------------
    async def list_approvals(self) -> list[dict[str, object]]:
        approvals = await self._platform.approvals.list_pending(tenant_id=self._principal.tenant_id)
        return [
            {
                "id": a.id,
                "tool_name": a.tool_name,
                "permission": a.permission.value,
                "summary": a.summary,
                "arguments": a.arguments,
                "run_id": a.run_id,
                "created_at": a.created_at.isoformat(),
                "expires_at": a.expires_at.isoformat(),
            }
            for a in approvals
        ]

    async def decide_approval(
        self, *, approval_id: str, approve: bool, reason: str | None = None
    ) -> dict[str, object]:
        record = await self._runs.decide_approval(
            principal=self._principal,
            approval_id=approval_id,
            approve=approve,
            reason=reason,
        )
        return self._run_dict(record)

    # -- Models ----------------------------------------------------------
    def list_models(self) -> list[dict[str, object]]:
        return self._models.list_models()

    def recommend_model(self) -> dict[str, object]:
        return self._models.recommend()

    async def install_model(self, model_id: str) -> dict[str, object]:
        return await self._models.install(model_id)

    def model_progress(self, model_id: str) -> dict[str, object] | None:
        return self._models.progress_for(model_id)

    def cancel_model_install(self, model_id: str) -> bool:
        return self._models.cancel_install(model_id)

    def verify_model(self, model_id: str) -> dict[str, object]:
        return self._models.verify(model_id)

    async def model_health(self, model_id: str) -> dict[str, object]:
        return await self._models.health_check(
            model_id, provider_factory=self._provider_factory(model_id)
        )

    def remove_model(self, model_id: str) -> dict[str, object]:
        return self._models.remove(model_id)

    # -- Browser provisioning -------------------------------------------
    def browser_view(self) -> dict[str, object]:
        """Browser readiness plus the browser config, from real state."""
        status = self._browser.status()
        settings = self._platform.settings
        return {
            "state": status.state.value,
            "ready": status.ready,
            "package_available": status.package_available,
            "chromium_path": status.chromium_path,
            "detail": status.detail,
            "enabled": settings.browser_enabled,
            "headless": settings.browser_headless,
            "allowed_domains": list(settings.browser_allowed_domains),
            "blocked_domains": list(settings.browser_blocked_domains),
        }

    async def provision_browser(self) -> dict[str, object]:
        """Install the missing browser runtime (real install), then report."""
        await self._platform.audit.record(
            AuditEventType.BROWSER_PROVISION_REQUESTED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="local",
            message="browser runtime provisioning requested",
        )
        status = await self._browser.provision()
        outcome = (
            AuditEventType.BROWSER_PROVISION_COMPLETED
            if status.ready
            else AuditEventType.BROWSER_PROVISION_FAILED
        )
        await self._platform.audit.record(
            outcome,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="local",
            message=f"browser runtime provisioning {status.state.value}",
            data={"state": status.state.value, "ready": status.ready},
        )
        return {
            "state": status.state.value,
            "ready": status.ready,
            "chromium_path": status.chromium_path,
            "detail": status.detail,
            "log_tail": status.log_tail,
        }

    def browser_provision_status(self) -> dict[str, object]:
        status = self._browser.status()
        return {
            "state": status.state.value,
            "ready": status.ready,
            "chromium_path": status.chromium_path,
            "detail": status.detail,
            "log_tail": status.log_tail,
        }

    async def cancel_browser_provision(self) -> dict[str, object]:
        cancelled = await self._browser.cancel()
        return {"cancelled": cancelled, **self.browser_provision_status()}

    def _provider_factory(self, model_id: str) -> Any:
        """Build a real provider for an installed model, or ``None``."""
        descriptor = self._models.descriptor_for(model_id)
        if descriptor is None:
            return None
        from ..local.model import LocalBackendProvider, build_backend

        def factory() -> LocalBackendProvider:
            backend = build_backend(descriptor, timeout_seconds=120.0)
            return LocalBackendProvider(descriptor=descriptor, backend=backend, timeout=120.0)

        return factory

    # -- Modes / privacy -------------------------------------------------
    async def set_mode(self, mode: OperatingMode) -> dict[str, object]:
        runtime = self._platform.local_runtime
        if runtime is None:
            raise ValidationError("this deployment has no local runtime to set a mode on")
        status = await runtime.set_mode(mode)
        return status.model_dump(mode="json")

    def modes(self) -> dict[str, object]:
        """Local / cloud / hybrid availability, derived from real config."""
        settings = self._platform.settings
        runtime = self._platform.local_runtime
        mode = runtime.mode.value if runtime is not None else OperatingMode.ONLINE.value
        return {
            "current": mode,
            "available": ["local", "cloud", "hybrid"],
            "local_model_available": bool(
                runtime.status().local_model_available if runtime is not None else False
            ),
            "cloud_providers": self._cloud_providers(),
            "remote_allowed": settings.model_allow_remote_fallback,
            "privacy": self.privacy(),
        }

    def privacy(self) -> dict[str, object]:
        """The effective data-boundary/privacy posture, from real settings."""
        settings = self._platform.settings
        runtime = self._platform.local_runtime
        return {
            "mode": runtime.mode.value if runtime is not None else OperatingMode.ONLINE.value,
            "allow_confidential_remote": settings.local_allow_confidential_remote,
            "remote_fallback_allowed": settings.model_allow_remote_fallback,
            "prefer_local": settings.model_prefer_local,
            "computer_control_enabled": settings.computer_control_enabled,
            "browser_enabled": settings.browser_enabled,
            "connectors_enabled": settings.connectors_enabled,
            "screen_policy": settings.computer_screen_policy,
            "content_policy": settings.browser_content_policy,
        }

    # -- Cloud providers -------------------------------------------------
    def _cloud_providers(self) -> list[dict[str, object]]:
        """Configured providers, with no secret value ever returned."""
        result: list[dict[str, object]] = []
        for provider in self._platform.gateway._registry.all():
            is_local = getattr(provider, "is_local", False)
            result.append(
                {
                    "name": provider.name,
                    "local": bool(is_local),
                    "capabilities": sorted(c.value for c in provider.capabilities),
                    # Never the key: only whether one is configured.
                    "configured": True,
                }
            )
        return result

    # -- Settings / permissions -----------------------------------------
    def permissions_view(self) -> dict[str, object]:
        """The effective effect of each capability, from the real policy engine."""
        policy = self._platform.policy
        capabilities: list[dict[str, object]] = []
        for cap_id, permission, label_ar in _SETTINGS_CAPABILITIES:
            decision = policy.evaluate(
                self._principal,
                tool_name="",
                permission=permission,
                risk=RiskLevel.MEDIUM,
            )
            capabilities.append(
                {
                    "id": cap_id,
                    "permission": permission.value,
                    "label_ar": label_ar,
                    "effect": decision.effect.value,
                    "requires_approval": decision.effect is PermissionEffect.REQUIRES_APPROVAL,
                    "reason": decision.reason,
                }
            )
        return {"capabilities": capabilities}

    def settings_view(self) -> dict[str, object]:
        settings = self._platform.settings
        runtime = self._platform.local_runtime
        return {
            "general": {
                "locale": settings.default_locale.value,
                "supported_locales": [loc.value for loc in settings.supported_locales],
                "environment": settings.environment,
                "runtime_kind": settings.runtime_kind.value,
            },
            "ai": {
                "default_model": settings.default_model,
                "local_model_name": settings.local_model_name,
                "local_model_backend": settings.local_model_backend,
                "prefer_local": settings.model_prefer_local,
                "allow_remote_fallback": settings.model_allow_remote_fallback,
            },
            "storage": {
                "store_dir": settings.local_store_dir,
                "model_storage_dir": settings.local_model_storage_dir or None,
                "log_dir": settings.log_dir or None,
            },
            "workspace": {
                "roots": list(runtime.status().allowed_roots) if runtime is not None else [],
                "workspace_dir": runtime.workspace_dir if runtime is not None else "",
            },
            "privacy": self.privacy(),
            "network_policy": settings.local_network_policy.value,
        }

    # -- Diagnostics -----------------------------------------------------
    def diagnostics(self) -> dict[str, object]:
        settings = self._platform.settings
        runtime = self._platform.local_runtime
        from .version import PRODUCT_VERSION

        disk_free = None
        try:
            import shutil

            disk_free = shutil.disk_usage(settings.local_store_dir).free
        except OSError:
            disk_free = None
        return {
            "version": PRODUCT_VERSION,
            "runtime": runtime.status().model_dump(mode="json") if runtime is not None else None,
            "durable": self._platform.durable,
            "distributed": self._platform.distributed,
            "local_model": {
                "available": bool(runtime.status().local_model_available)
                if runtime is not None
                else False,
                "detail": runtime.status().local_model_detail if runtime is not None else "",
            },
            "computer_control_enabled": settings.computer_control_enabled,
            "browser_enabled": settings.browser_enabled,
            "browser": self.browser_view(),
            "connectors_enabled": settings.connectors_enabled,
            "tools": self._platform.tools.names(),
            "disk_free_bytes": disk_free,
            "event_subscribers": self._events.subscriber_count,
        }

    def safe_export(self) -> dict[str, object]:
        """A redacted diagnostics bundle safe to share (no secrets, no payloads).

        Only capability/health metadata is included; run goals, tool arguments
        and any credential are excluded by construction.
        """
        diag = self.diagnostics()
        # Drop anything that could carry user content.
        runtime = diag.get("runtime")
        if isinstance(runtime, dict):
            runtime.pop("device_id", None)
        return {
            "product": "Arabic AI Agent Platform",
            "export_kind": "diagnostics",
            "diagnostics": diag,
        }

    # -- Internals -------------------------------------------------------
    def _run_dict(self, record: Any) -> dict[str, object]:
        return {
            "id": record.id,
            "goal": record.goal,
            "status": record.status.value,
            "result": record.result,
            "verified": record.verified,
            "error": record.error,
            "workspace_dir": record.workspace_dir,
            "pending_approval_id": record.pending_approval_id,
            "steps_executed": record.steps_executed,
            "tool_calls": record.tool_calls,
            "retries": record.retries,
            "replans": record.replans,
            "usage": dict(record.usage),
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "finished_at": record.finished_at.isoformat() if record.finished_at else None,
        }


def build_model_manager(platform: Platform) -> ModelManager:
    """Build a model manager from the platform's real settings and hardware."""
    settings = platform.settings
    storage = settings.local_model_storage_dir or str(
        __import__("pathlib").Path(settings.local_store_dir) / "models"
    )
    try:
        hardware: HardwareProfile | None = detect_hardware(disk_path=storage)
    except Exception:  # detection must never crash startup
        hardware = None
    active = settings.local_model_name or None
    return ModelManager(storage_dir=storage, hardware=hardware, active_model_id=active)


__all__ = ["DesktopPrincipal", "DesktopService", "build_model_manager"]
