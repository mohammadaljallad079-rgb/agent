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

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
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
from ..core.errors import NotFoundError, PlatformError, ValidationError
from ..desktop.events import DesktopEventBroker
from ..desktop.model_manager import ModelManager, read_active_model, write_active_model
from ..local.hardware import HardwareProfile, detect_hardware
from ..local.model_security import resolve_storage_dir
from ..models.base import ChatMessage, ModelRequest
from ..permissions.base import Permission, Principal

logger = logging.getLogger(__name__)

#: Wall-clock bound for the routed inference acceptance test. A CPU-only model
#: answering one short prompt can legitimately take a while, but the test must
#: never hang the UI forever.
_ACCEPTANCE_TIMEOUT_SECONDS = 120.0

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
    ("calendar.read", Permission.CALENDAR_READ, "قراءة التقويم"),
    ("calendar.write", Permission.CALENDAR_WRITE, "تعديل التقويم"),
    ("calendar.delete", Permission.CALENDAR_DELETE, "حذف أحداث التقويم"),
    ("printing", Permission.PRINT_DOCUMENT, "الطباعة"),
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
        #: True only after a real health check passed for the active model.
        self._active_model_ready = False
        #: True only after the routed inference acceptance test produced output.
        self._inference_verified = False

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

    async def test_model_inference(self, model_id: str | None = None) -> dict[str, object]:
        """Prove the *routed* model answers: a genuine inference acceptance test.

        Unlike :meth:`model_health`, which loads the file-backed backend for one
        installed model, this runs the request through the live gateway exactly
        as a user task would — so it exercises the active model, the routing
        policy and the provider the agent actually uses. It is the acceptance
        check the UI offers after activation, and it never reports success
        without real model output. ``passed`` is false with a reason when there
        is no runnable model (e.g. the setup-required placeholder) rather than a
        fabricated pass.
        """
        target = model_id or self._models.active_model_id()
        request = ModelRequest(
            messages=[ChatMessage.user("Reply with the single word: ready")],
            max_tokens=16,
        )
        try:
            response = await asyncio.wait_for(
                self._platform.gateway.complete(request), timeout=_ACCEPTANCE_TIMEOUT_SECONDS
            )
        except (TimeoutError, PlatformError) as exc:
            detail = getattr(exc, "message", None) or str(exc)
            self._inference_verified = False
            return {
                "model_id": target,
                "passed": False,
                "detail": f"inference test failed: {detail}",
            }
        text = (response.message.content or "").strip()
        passed = bool(text)
        self._inference_verified = passed
        return {
            "model_id": target,
            "passed": passed,
            "detail": "model produced output" if passed else "model returned no output",
            "output_chars": len(text),
            "model": response.model,
            "usage": {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
            },
        }

    async def model_health(self, model_id: str) -> dict[str, object]:
        return await self._models.health_check(
            model_id, provider_factory=self._provider_factory(model_id)
        )

    def remove_model(self, model_id: str) -> dict[str, object]:
        return self._models.remove(model_id)

    # -- Model activation ------------------------------------------------
    def active_model(self) -> dict[str, object]:
        """The active local model plus whether it is real and ready to run.

        ``ready`` is only ever true after a genuine health check has passed; a
        model that is merely selected is reported ``selected`` but not ready.
        """
        model_id = self._models.active_model_id()
        if model_id is None:
            return {
                "model_id": None,
                "selected": False,
                "installed": False,
                "ready": False,
                "inference_verified": False,
            }
        verdict = self._models.can_activate(model_id)
        installed = bool(verdict["ok"])
        return {
            "model_id": model_id,
            "selected": True,
            "installed": installed,
            "installed_reason": verdict["reason"],
            "ready": self._active_model_ready,
            "inference_verified": self._inference_verified,
        }

    def can_activate_model(self, model_id: str) -> dict[str, object]:
        return self._models.can_activate(model_id)

    async def activate_model(self, model_id: str) -> dict[str, object]:
        """Select an installed model, health-check it, and make it the runtime's.

        Activation is real, not a flag: the model must be installed, must pass a
        genuine health check (load + tiny inference), and only then is it
        registered with the running gateway as the preferred local model. A
        model that fails the health check is *not* activated and the previous
        selection is left untouched.
        """
        verdict = self._models.can_activate(model_id)
        if not verdict["ok"]:
            raise NotFoundError(str(verdict["reason"]), details={"model_id": model_id})

        health = await self._models.health_check(
            model_id, provider_factory=self._provider_factory(model_id)
        )
        if health.get("health") != "healthy":
            return {
                "model_id": model_id,
                "active": False,
                "reason": "health check did not pass",
                "health": health,
            }

        result = self._models.activate(
            model_id,
            persist=lambda mid: write_active_model(self._model_storage_dir(), mid),
        )
        self._active_model_ready = True
        # A freshly activated model has not yet passed the routed acceptance
        # test; the UI must re-run it rather than inherit a stale pass.
        self._inference_verified = False
        self._register_active_local_model(model_id)
        await self._platform.audit.record(
            AuditEventType.MODEL_ACTIVATED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="local",
            message="local model activated",
            data={"model_id": model_id},
        )
        return {**result, "health": health}

    async def deactivate_model(self) -> dict[str, object]:
        result = self._models.deactivate()
        self._active_model_ready = False
        self._inference_verified = False
        write_active_model(self._model_storage_dir(), None)
        await self._platform.audit.record(
            AuditEventType.MODEL_DEACTIVATED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id="local",
            message="local model deactivated",
            data={"model_id": result.get("previous_model_id")},
        )
        return result

    async def setup_local_model(self, *, model_id: str | None = None) -> dict[str, object]:
        """First-run: make a local model installed *and* active in one step.

        This is the whole first-run path over the existing components — the
        hardware-gated :meth:`ModelManager.recommend` to choose a model, the real
        resumable installer, the real health check and the real gateway
        registration — with no new mechanism of its own. It is idempotent: an
        already-installed model is activated without re-downloading, and an
        already-active, healthy model is a no-op. When no model id is given the
        recommendation is used, and a model the hardware cannot run is refused by
        the installer rather than silently attempted.
        """
        chosen = model_id
        recommended = False
        if not chosen:
            recommendation = self._models.recommend()
            candidate = recommendation.get("model_id")
            chosen = str(candidate) if candidate else ""
            recommended = True
        if not chosen:
            raise ValidationError("no local model is available to set up")

        already_installed = bool(self._models.can_activate(chosen)["ok"])
        installed_now = False
        if not already_installed:
            install = await self._models.install(chosen)
            install_result = install["install"]
            assert isinstance(install_result, dict)
            if not install_result["success"]:
                return {
                    "model_id": chosen,
                    "recommended": recommended,
                    "installed": False,
                    "installed_now": False,
                    "active": False,
                    "detail": str(install_result.get("detail") or "installation failed"),
                    "readiness": await self.model_readiness(),
                }
            installed_now = True

        activation = await self.activate_model(chosen)
        return {
            "model_id": chosen,
            "recommended": recommended,
            "installed": True,
            "installed_now": installed_now,
            "active": bool(activation.get("active")),
            "health": activation.get("health"),
            "detail": str(activation.get("reason") or ""),
            "readiness": await self.model_readiness(),
        }

    async def model_readiness(self) -> dict[str, object]:
        """A real, end-to-end readiness report for running the agent locally.

        This is the first-run check: it reports whether the runtime has a local
        model selected, installed, and able to answer — and what to do when it
        does not. It never claims readiness without a passing health check.
        """
        active = self.active_model()
        runtime = self._platform.local_runtime
        mode = runtime.mode.value if runtime is not None else OperatingMode.ONLINE.value
        if not active["selected"]:
            return {
                **active,
                "mode": mode,
                "next_step": "install_and_activate",
                "detail": "no local model selected",
            }
        if not active["installed"]:
            return {
                **active,
                "mode": mode,
                "next_step": "install",
                "detail": str(active.get("installed_reason") or "model is not installed"),
            }
        health = await self._models.health_check(
            str(active["model_id"]),
            provider_factory=self._provider_factory(str(active["model_id"])),
        )
        self._active_model_ready = health.get("health") == "healthy"
        return {
            **active,
            "ready": self._active_model_ready,
            "mode": mode,
            "health": health,
            "next_step": "" if self._active_model_ready else "diagnose",
            "detail": str(health.get("detail") or ""),
        }

    def _model_storage_dir(self) -> Path:
        settings = self._platform.settings
        return Path(
            settings.local_model_storage_dir or str(Path(settings.local_store_dir) / "models")
        )

    def _register_active_local_model(self, model_id: str) -> None:
        """Register the activated model with the running gateway.

        The registry is keyed by provider name; re-registering under the same
        name (``local``) with ``replace=True`` swaps the provider so the agent
        uses the newly activated weights without an agent-core change.
        """
        factory = self._provider_factory(model_id)
        if factory is None:
            return
        provider = factory()
        descriptor = self._models.descriptor_for(model_id)
        if descriptor is None:
            return
        from ..local.model_registry import model_logical_id

        registry = self._platform.gateway.registry
        registry.register(provider, replace=True)
        self._platform.local_model_provider = provider
        logical = model_logical_id(descriptor)
        # Point the gateway at the activated model so the *agent* runs it, and
        # keep the router aware this id is local so offline/prefer-local routing
        # can select it. The previous default (e.g. the setup placeholder) stays
        # as a fallback, harmless because the router pins local-required traffic
        # to local candidates only.
        self._platform.gateway.set_default_model(logical)
        router = self._platform.gateway.router
        if router is not None:
            router.add_local_model(logical)

    # -- Capabilities / tools -------------------------------------------
    def tools_view(self) -> dict[str, object]:
        """The tools the running agent actually has, with their real permissions.

        This is a read view over the live tool registry — the same registry the
        agent uses. It never lists a tool the runtime did not register.
        """
        tools = []
        for tool in self._platform.tools.all():
            perms = sorted(p.value for p in tool.permissions)
            risks = {p.value: r.value for p, r in tool.permission_risks().items()}
            tools.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "permissions": perms,
                    "risks": risks,
                    "side_effecting": bool(getattr(tool, "side_effecting", False)),
                }
            )
        tools.sort(key=lambda t: str(t["name"]))
        return {"tools": tools, "count": len(tools)}

    # -- Generated artifacts ---------------------------------------------
    #: Formats the UI can open in the operating system's default application.
    _OPENABLE_SUFFIXES = frozenset({".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".md", ".txt"})
    #: Extensions that select a document the runtime can generate.
    _ARTIFACT_SUFFIXES = frozenset(
        {".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".md", ".markdown", ".txt", ".html", ".json"}
    )

    def _run_workspace(self, record: Any) -> Path | None:
        """The run's workspace, resolved through the filesystem sandbox."""
        from ..local.sandbox import FilesystemPolicy, FilesystemSandbox

        workspace = str(getattr(record, "workspace_dir", "") or "")
        if not workspace:
            return None
        sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(workspace))
        try:
            return sandbox.resolve_for_read(".")
        except Exception:
            return None

    def _list_artifacts(self, root: Path, *, run_id: str) -> list[dict[str, Any]]:
        """Real generated files under a run workspace, newest first."""
        artifacts: list[dict[str, Any]] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix not in self._ARTIFACT_SUFFIXES:
                continue
            try:
                stat = path.stat()
            except OSError:  # a file that vanished between glob and stat
                continue
            try:
                relative = path.relative_to(root).as_posix()
            except ValueError:
                continue
            artifacts.append(
                {
                    "name": path.name,
                    "path": relative,
                    "format": suffix.lstrip("."),
                    "bytes": stat.st_size,
                    "modified_at": stat.st_mtime,
                    "openable": suffix in self._OPENABLE_SUFFIXES,
                    "run_id": run_id,
                }
            )
        artifacts.sort(key=lambda a: float(a["modified_at"]), reverse=True)
        return artifacts

    async def run_artifacts(self, run_id: str) -> dict[str, object]:
        """Documents a run generated into its workspace.

        This is a real directory listing of the run's sandboxed workspace,
        filtered to generatable document types — not a database of claims. It
        never leaves the workspace, so it cannot expose host files.
        """
        record = await self._runs.get(run_id, tenant_id=self._principal.tenant_id)
        root = self._run_workspace(record)
        if root is None or not root.is_dir():
            return {"run_id": run_id, "artifacts": [], "count": 0}
        artifacts = self._list_artifacts(root, run_id=run_id)
        return {"run_id": run_id, "artifacts": artifacts, "count": len(artifacts)}

    async def list_artifacts(self, *, limit: int = 20) -> dict[str, object]:
        """Generated documents across the most recent runs, newest first."""
        runs = await self._runs.list(tenant_id=self._principal.tenant_id, limit=limit)
        collected: list[dict[str, object]] = []
        for record in runs:
            root = self._run_workspace(record)
            if root is None or not root.is_dir():
                continue
            collected.extend(self._list_artifacts(root, run_id=record.id))
        collected.sort(key=lambda a: float(str(a["modified_at"])), reverse=True)
        return {"artifacts": collected, "count": len(collected)}

    async def open_artifact(self, run_id: str, relative_path: str) -> dict[str, object]:
        """Open a generated artifact in the OS default application.

        The path is resolved through the run's filesystem sandbox, so only a
        real file inside that run's workspace can be opened — a ``..`` escape or
        an absolute path is refused. The operating system's own launcher is used
        (``os.startfile`` on Windows, ``xdg-open``/``open`` elsewhere); a missing
        launcher is reported honestly rather than pretending the file opened.
        """
        record = await self._runs.get(run_id, tenant_id=self._principal.tenant_id)
        root = self._run_workspace(record)
        if root is None or not root.is_dir():
            raise NotFoundError("run workspace is not available", details={"run_id": run_id})
        from ..local.sandbox import FilesystemPolicy, FilesystemSandbox

        sandbox = FilesystemSandbox(FilesystemPolicy.workspace_only(str(root)))
        resolved = sandbox.resolve_for_read(relative_path)
        if not resolved.is_file():
            raise NotFoundError(
                "artifact does not exist", details={"run_id": run_id, "path": relative_path}
            )
        if resolved.suffix.lower() not in self._OPENABLE_SUFFIXES:
            raise ValidationError(
                "this artifact type cannot be opened in a system application",
                details={"path": relative_path},
            )
        await self._platform.audit.record(
            AuditEventType.ARTIFACT_OPENED,
            tenant_id=self._principal.tenant_id,
            user_id=self._principal.user_id,
            run_id=run_id,
            message="generated artifact opened in the system application",
            data={"path": relative_path},
        )
        opened = _launch_with_system(resolved)
        return {"run_id": run_id, "path": relative_path, "opened": opened}

    # -- Workflows -------------------------------------------------------
    async def list_workflows(self, *, limit: int = 50) -> list[dict[str, object]]:
        records = await self._platform.workflows.list_workflows(
            tenant_id=self._principal.tenant_id, limit=limit
        )
        return [
            {
                "id": w.id,
                "name": w.name,
                "description": w.description,
                "status": w.status.value,
                "latest_version": w.latest_version,
                "published": w.is_published,
                "updated_at": w.updated_at.isoformat() if w.updated_at else None,
            }
            for w in records
        ]

    async def list_workflow_runs(
        self, workflow_id: str, *, limit: int = 20
    ) -> list[dict[str, object]]:
        runs = await self._platform.workflow_runs.list_runs(
            tenant_id=self._principal.tenant_id, workflow_id=workflow_id, limit=limit
        )
        return [
            {
                "id": r.id,
                "status": getattr(r.status, "value", str(r.status)),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in runs
        ]

    # -- Connectors ------------------------------------------------------
    def connectors_view(self) -> dict[str, object]:
        """Configured connectors with their real, reported health.

        An unconfigured or unreachable connector is reported as such; nothing
        here claims a connection the runtime did not make.
        """
        runtime = self._platform.connector_runtime
        if runtime is None:
            return {"enabled": False, "connectors": [], "count": 0}
        connectors = []
        for connector in runtime.registry.all():
            info = connector.info()
            connectors.append(
                {
                    "name": info.name,
                    "kind": info.kind.value,
                    "state": info.state.value,
                    "capabilities": sorted(c.value for c in info.capabilities),
                    "write_capable": info.write_capable,
                    "approval_required": info.approval_required,
                    "authenticated": info.authenticated,
                    "detail": info.detail,
                }
            )
        return {
            "enabled": self._platform.settings.connectors_enabled,
            "connectors": connectors,
            "count": len(connectors),
        }

    # -- Computer control ------------------------------------------------
    def computer_view(self) -> dict[str, object]:
        """Computer-control availability from real settings and runtime state."""
        settings = self._platform.settings
        runtime = self._platform.computer_runtime
        enabled = bool(settings.computer_control_enabled)
        return {
            "enabled": enabled,
            "available": runtime is not None,
            "screen_policy": settings.computer_screen_policy,
            "pointer_allowed": enabled and runtime is not None,
            "keyboard_allowed": enabled and runtime is not None,
        }

    # -- Printing --------------------------------------------------------
    def printing_view(self) -> dict[str, object]:
        """Printing availability from real settings, runtime and spooler state.

        Nothing here claims a printer: ``enabled`` reflects the setting, and
        ``available`` is true only when a real print runtime is wired and its
        backend reports itself usable. ``unavailable_reason`` carries the
        backend's honest explanation when it is not.
        """
        settings = self._platform.settings
        runtime = self._platform.print_runtime
        enabled = bool(settings.printing_enabled)
        available = bool(runtime is not None and runtime.available)
        return {
            "enabled": enabled,
            "available": available,
            "backend": runtime.backend.name if runtime is not None else "",
            "unavailable_reason": (runtime.unavailable_reason if runtime is not None else ""),
            "max_document_bytes": settings.print_max_document_bytes,
            "max_jobs_per_run": settings.print_max_jobs_per_run,
            "min_job_interval_seconds": settings.print_min_job_interval_seconds,
        }

    async def list_print_jobs(self, *, limit: int = 50) -> dict[str, object]:
        """Persisted print-job metadata for this tenant, most recent first.

        Returns a real (possibly empty) list from the job store; when printing
        is disabled there is no store, so the list is honestly empty rather than
        fabricated. Only safe metadata is returned — never document bytes.
        """
        runtime = self._platform.print_runtime
        if runtime is None:
            return {"enabled": False, "jobs": [], "count": 0}
        records = await runtime.list_jobs(tenant_id=self._principal.tenant_id, limit=limit)
        jobs = [
            {
                "job_id": r.id,
                "printer_id": r.printer_id,
                "title": r.title,
                "status": r.status.value,
                "observed": r.observed,
                "size_bytes": r.size_bytes,
                "run_id": r.run_id,
                "created_at": r.created_at.isoformat(),
            }
            for r in records
        ]
        return {"enabled": True, "jobs": jobs, "count": len(jobs)}

    # -- Memory ----------------------------------------------------------
    async def list_memory(self, *, limit: int = 50) -> dict[str, object]:
        """The tenant's stored memory entries, newest first.

        Memory is tenant-scoped; this never returns another tenant's entries.
        """
        from ..memory.base import MemoryQuery

        query = MemoryQuery(tenant_id=self._principal.tenant_id, limit=limit)
        entries = await self._platform.memory.query(query)
        return {
            "entries": [
                {
                    "id": e.id,
                    "kind": e.kind.value,
                    "key": e.key,
                    "content": e.content,
                    "importance": e.importance,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in entries
            ],
            "count": len(entries),
        }

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
    def providers_view(self) -> dict[str, object]:
        """Configured model providers, with their real config and no secrets."""
        providers = self._cloud_providers()
        return {
            "providers": providers,
            "default_model": self._platform.gateway.default_model,
            "fallbacks": list(self._platform.gateway.fallbacks),
            "needs_setup": any(p.get("needs_setup") for p in providers),
            "active_model": self.active_model(),
        }

    def _cloud_providers(self) -> list[dict[str, object]]:
        """Configured providers, with no secret value ever returned."""
        result: list[dict[str, object]] = []
        for provider in self._platform.gateway.registry.all():
            is_local = getattr(provider, "is_local", False)
            needs_setup = provider.name == "setup"
            result.append(
                {
                    "name": provider.name,
                    "local": bool(is_local),
                    "capabilities": sorted(c.value for c in provider.capabilities),
                    "model": self._provider_model(provider.name),
                    "base_url": self._provider_base_url(provider.name),
                    # Never the key: only whether one is configured.
                    "configured": not needs_setup,
                    "needs_setup": needs_setup,
                    "detail": getattr(provider, "detail", "") if needs_setup else "",
                    "is_default": self._platform.gateway.default_model.startswith(
                        f"{provider.name}/"
                    ),
                }
            )
        return result

    def _provider_base_url(self, name: str) -> str:
        settings = self._platform.settings
        if name == "openai":
            return settings.openai_base_url
        if name == "anthropic":
            return settings.anthropic_base_url
        if name == "local":
            return settings.local_model_base_url
        return ""

    def _provider_model(self, name: str) -> str:
        """The model id a provider is configured with (never a secret)."""
        settings = self._platform.settings
        if name == "anthropic":
            return settings.anthropic_model
        if name == "local":
            return settings.local_model_name
        if name == "openai":
            default = settings.default_model
            return default.split("/", 1)[1] if default.startswith("openai/") else default
        return ""

    async def test_provider(self, name: str) -> dict[str, object]:
        """Send one bounded real completion to a configured provider.

        This is a genuine connectivity + credential check: it calls the
        provider's own ``complete`` with a tiny prompt and reports the real
        outcome and latency. It never returns the key. A failure is reported as
        an honest ``ok: false`` with the provider's message (redacted), not a
        fabricated success.
        """
        from ..core.redaction import redact
        from ..models.base import ChatMessage, ModelRequest

        registry = self._platform.gateway._registry
        if not registry.has(name):
            raise NotFoundError(
                f"provider is not configured: {name}", details={"known": registry.names()}
            )
        provider = registry.get(name)
        request = ModelRequest(
            messages=(ChatMessage.user("ping"),),
            model=self._provider_model(name),
            max_tokens=8,
        )
        started = time.perf_counter()
        try:
            response = await asyncio.wait_for(provider.complete(request), timeout=30.0)
        except TimeoutError:
            return {"provider": name, "ok": False, "error": "timed out after 30s"}
        except PlatformError as exc:
            return {"provider": name, "ok": False, "error": redact({"e": exc.message})["e"]}
        except Exception as exc:  # a connectivity check must never crash the app
            return {"provider": name, "ok": False, "error": redact({"e": str(exc)})["e"]}
        latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
        return {
            "provider": name,
            "ok": True,
            "model": response.model,
            "latency_ms": latency_ms,
            "finish_reason": response.finish_reason,
        }

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
            "printing_enabled": settings.printing_enabled,
            "printing": self.printing_view(),
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


def _launch_with_system(path: Path) -> bool:
    """Open ``path`` in the OS default application. Returns whether it launched.

    The desktop UI runs inside a native webview with no network origin, so it
    cannot open a file itself; this is the one place a generated artifact is
    handed to the operating system. A missing launcher is reported as ``False``
    rather than raising, so the UI can say so honestly.
    """
    import subprocess
    import sys

    try:
        if sys.platform.startswith("win"):
            import os

            # ``os.startfile`` exists only on Windows; resolving it via
            # ``getattr`` keeps the behaviour identical without a platform-
            # specific ignore that POSIX mypy would flag as unused.
            startfile = getattr(os, "startfile", None)
            if startfile is None:
                return False
            startfile(str(path))
            return True
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen(  # noqa: S603 - fixed opener, no shell, sandbox-resolved path
            [opener, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        logger.warning("could not open artifact in a system application", exc_info=True)
        return False


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
    # The persisted activation wins over the configured name, so a model the
    # user selected in the UI is still active after a restart. The configured
    # name is the fallback for a deployment that pins a model via AAP_* — but
    # only when that model is actually installed, so a first run is not
    # misreported as having an active model that does not exist.
    persisted = read_active_model(resolve_storage_dir(storage))
    manager = ModelManager(storage_dir=storage, hardware=hardware)
    configured = settings.local_model_name or None
    if persisted and manager.can_activate(persisted)["ok"]:
        active = persisted
    elif configured and manager.can_activate(configured)["ok"]:
        active = configured
    else:
        active = None
    return ModelManager(storage_dir=storage, hardware=hardware, active_model_id=active)


__all__ = ["DesktopPrincipal", "DesktopService", "build_model_manager"]
