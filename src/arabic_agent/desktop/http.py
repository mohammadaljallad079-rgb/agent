"""HTTP surface for the desktop product.

Mounted beside the existing local control API, bound to loopback and protected
by the same ``X-Local-Token`` header. It is the *only* surface the desktop UI
talks to, and it never executes a tool directly: every operation delegates to
:class:`~arabic_agent.desktop.service.DesktopService`, which itself goes through
the existing run service, policy engine, approvals and audit.

Endpoints (all under ``/desktop``):

* ``GET  /desktop/bootstrap``      — one call to populate the shell on launch
* ``GET  /desktop/runs``           — list durable sessions/tasks
* ``POST /desktop/runs``           — start a goal as a durable run
* ``GET  /desktop/runs/{id}``      — one run
* ``GET  /desktop/runs/{id}/timeline`` — the run's audit trail
* ``POST /desktop/runs/{id}/cancel``   — emergency stop
* ``GET  /desktop/events``         — Server-Sent Events stream (real runtime events)
* ``GET  /desktop/approvals``      — pending approvals
* ``POST /desktop/approvals/{id}`` — approve / deny
* ``GET  /desktop/models``         — catalogue + installed state + active model
* ``GET  /desktop/models/readiness`` — real first-run readiness report
* ``POST /desktop/models/{id}/install`` / ``GET .../progress`` / ``POST .../cancel``
* ``POST /desktop/models/{id}/activate`` / ``POST /desktop/models/active/deactivate``
* ``POST /desktop/models/{id}/verify`` / ``POST .../health`` / ``DELETE /desktop/models/{id}``
* ``GET  /desktop/modes`` / ``POST /desktop/modes``
* ``GET  /desktop/tools``          — the live tool registry with permissions
* ``GET  /desktop/artifacts`` / ``GET /desktop/runs/{id}/artifacts``
* ``POST /desktop/runs/{id}/artifacts/open`` — open a generated document
* ``GET  /desktop/workflows`` / ``GET /desktop/workflows/{id}/runs``
* ``GET  /desktop/connectors`` / ``GET /desktop/computer`` / ``GET /desktop/memory``
* ``GET  /desktop/settings`` / ``GET /desktop/permissions``
* ``GET  /desktop/diagnostics`` / ``GET /desktop/diagnostics/export``
* ``GET  /desktop/version``
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends, FastAPI, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..core.enums import OperatingMode
from ..core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
    ValidationError,
)
from .events import sse_format
from .service import DesktopService
from .version import PRODUCT_VERSION


class StartRunRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8000)
    workspace_dir: str | None = None


class ApprovalDecision(BaseModel):
    approve: bool
    reason: str | None = None


class ModeRequest(BaseModel):
    mode: OperatingMode


class OpenArtifactRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1024)


class SetupModelRequest(BaseModel):
    #: Optional explicit catalogue id; omitted means "use the recommendation".
    model_id: str | None = Field(default=None, max_length=200)


class TestInferenceRequest(BaseModel):
    #: Optional model id; omitted means "the active model".
    model_id: str | None = Field(default=None, max_length=200)


def create_desktop_app(
    service: DesktopService,
    *,
    authorize: Any,
    tenant_id: str = "local",
) -> FastAPI:
    """Build the desktop app, reusing the control app's ``authorize`` dependency."""
    app = FastAPI(
        title="Arabic AI Agent — Desktop",
        version=PRODUCT_VERSION,
        description="Product surface for the Arabic-first agent desktop.",
    )

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_request: object, exc: PlatformError) -> object:
        from fastapi.responses import JSONResponse

        if isinstance(exc, PermissionDeniedError):
            code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, ValidationError):
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        elif isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ConflictError):
            code = status.HTTP_409_CONFLICT
        else:
            code = status.HTTP_500_INTERNAL_SERVER_ERROR
        return JSONResponse(status_code=code, content={"detail": exc.message})

    @app.get("/desktop/version")
    async def get_version() -> dict[str, str]:
        return {"version": PRODUCT_VERSION}

    @app.get("/desktop/bootstrap")
    async def bootstrap(_: None = Depends(authorize)) -> dict[str, object]:
        runs = await service.list_runs(limit=20)
        return {
            "version": PRODUCT_VERSION,
            "modes": service.modes(),
            "models": service.list_models(),
            "recommendation": service.recommend_model(),
            "active_model": service.active_model(),
            "readiness": await service.model_readiness(),
            "runs": runs,
            "approvals": await service.list_approvals(),
            "permissions": service.permissions_view(),
            "settings": service.settings_view(),
            "browser": service.browser_view(),
            "providers": service.providers_view(),
            "tools": service.tools_view(),
            "artifacts": await service.list_artifacts(),
            "connectors": service.connectors_view(),
            "computer": service.computer_view(),
            "printing": service.printing_view(),
            "printJobs": await service.list_print_jobs(),
            "workflows": await service.list_workflows(),
            "memory": await service.list_memory(),
            "diagnostics": service.diagnostics(),
        }

    # -- Runs / sessions -------------------------------------------------
    @app.get("/desktop/runs")
    async def list_runs(
        limit: int = 50, offset: int = 0, _: None = Depends(authorize)
    ) -> list[dict[str, object]]:
        return await service.list_runs(limit=limit, offset=offset)

    @app.post("/desktop/runs", status_code=status.HTTP_201_CREATED)
    async def start_run(
        payload: StartRunRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await service.start_run(goal=payload.goal, workspace_dir=payload.workspace_dir)

    @app.get("/desktop/runs/{run_id}")
    async def get_run(run_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.get_run(run_id)

    @app.get("/desktop/runs/{run_id}/timeline")
    async def run_timeline(run_id: str, _: None = Depends(authorize)) -> list[dict[str, object]]:
        return await service.run_timeline(run_id)

    @app.post("/desktop/runs/{run_id}/cancel")
    async def cancel_run(run_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.cancel_run(run_id)

    # -- Live events -----------------------------------------------------
    @app.get("/desktop/events")
    async def events(run_id: str | None = None, _: None = Depends(authorize)) -> StreamingResponse:
        async def generate() -> AsyncIterator[str]:
            yield ": connected\n\n"
            try:
                async for event in service.events.stream(tenant_id=tenant_id):
                    if run_id is not None and event.run_id != run_id:
                        continue
                    yield sse_format(event)
            except asyncio.CancelledError:  # client disconnected
                return

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- Approvals -------------------------------------------------------
    @app.get("/desktop/approvals")
    async def list_approvals(_: None = Depends(authorize)) -> list[dict[str, object]]:
        return await service.list_approvals()

    @app.post("/desktop/approvals/{approval_id}")
    async def decide_approval(
        approval_id: str, payload: ApprovalDecision, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await service.decide_approval(
            approval_id=approval_id, approve=payload.approve, reason=payload.reason
        )

    # -- Models ----------------------------------------------------------
    @app.get("/desktop/models")
    async def list_models(_: None = Depends(authorize)) -> dict[str, object]:
        return {
            "models": service.list_models(),
            "recommendation": service.recommend_model(),
            "active": service.active_model(),
        }

    @app.get("/desktop/models/readiness")
    async def model_readiness(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.model_readiness()

    @app.post("/desktop/models/{model_id}/install", status_code=status.HTTP_202_ACCEPTED)
    async def install_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.install_model(model_id)

    @app.post("/desktop/models/{model_id}/activate")
    async def activate_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.activate_model(model_id)

    @app.post("/desktop/models/active/deactivate")
    async def deactivate_model(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.deactivate_model()

    @app.post("/desktop/models/setup")
    async def setup_local_model(
        payload: SetupModelRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await service.setup_local_model(model_id=payload.model_id)

    @app.get("/desktop/models/{model_id}/progress")
    async def model_progress(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        progress = service.model_progress(model_id)
        return {"progress": progress}

    @app.post("/desktop/models/{model_id}/cancel")
    async def cancel_model_install(
        model_id: str, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return {"cancelled": service.cancel_model_install(model_id)}

    @app.post("/desktop/models/{model_id}/verify")
    async def verify_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return service.verify_model(model_id)

    @app.post("/desktop/models/{model_id}/health")
    async def model_health(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.model_health(model_id)

    @app.post("/desktop/models/test-inference")
    async def test_model_inference(
        payload: TestInferenceRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await service.test_model_inference(payload.model_id)

    @app.delete("/desktop/models/{model_id}")
    async def remove_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return service.remove_model(model_id)

    # -- Modes / privacy / settings --------------------------------------
    @app.get("/desktop/modes")
    async def get_modes(_: None = Depends(authorize)) -> dict[str, object]:
        return service.modes()

    @app.get("/desktop/providers")
    async def list_providers(_: None = Depends(authorize)) -> dict[str, object]:
        return service.providers_view()

    @app.post("/desktop/providers/{provider_name}/test")
    async def test_provider(provider_name: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.test_provider(provider_name)

    @app.post("/desktop/modes")
    async def set_mode(payload: ModeRequest, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.set_mode(payload.mode)

    # -- Browser provisioning --------------------------------------------
    @app.get("/desktop/browser")
    async def browser_status(_: None = Depends(authorize)) -> dict[str, object]:
        return service.browser_view()

    @app.post("/desktop/browser/provision", status_code=status.HTTP_202_ACCEPTED)
    async def provision_browser(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.provision_browser()

    @app.get("/desktop/browser/progress")
    async def browser_progress(_: None = Depends(authorize)) -> dict[str, object]:
        return service.browser_provision_status()

    @app.post("/desktop/browser/cancel")
    async def cancel_browser(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.cancel_browser_provision()

    @app.get("/desktop/settings")
    async def get_settings(_: None = Depends(authorize)) -> dict[str, object]:
        return service.settings_view()

    @app.get("/desktop/permissions")
    async def get_permissions(_: None = Depends(authorize)) -> dict[str, object]:
        return service.permissions_view()

    # -- Capabilities / tools --------------------------------------------
    @app.get("/desktop/tools")
    async def get_tools(_: None = Depends(authorize)) -> dict[str, object]:
        return service.tools_view()

    # -- Generated artifacts ---------------------------------------------
    @app.get("/desktop/artifacts")
    async def list_artifacts(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.list_artifacts()

    @app.get("/desktop/runs/{run_id}/artifacts")
    async def run_artifacts(run_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.run_artifacts(run_id)

    @app.post("/desktop/runs/{run_id}/artifacts/open")
    async def open_artifact(
        run_id: str, payload: OpenArtifactRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await service.open_artifact(run_id, payload.path)

    # -- Workflows -------------------------------------------------------
    @app.get("/desktop/workflows")
    async def list_workflows(_: None = Depends(authorize)) -> dict[str, object]:
        return {"workflows": await service.list_workflows()}

    @app.get("/desktop/workflows/{workflow_id}/runs")
    async def list_workflow_runs(
        workflow_id: str, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return {"runs": await service.list_workflow_runs(workflow_id)}

    # -- Connectors ------------------------------------------------------
    @app.get("/desktop/connectors")
    async def get_connectors(_: None = Depends(authorize)) -> dict[str, object]:
        return service.connectors_view()

    # -- Computer control -------------------------------------------------
    @app.get("/desktop/computer")
    async def get_computer(_: None = Depends(authorize)) -> dict[str, object]:
        return service.computer_view()

    # -- Printing --------------------------------------------------------
    @app.get("/desktop/printing")
    async def get_printing(_: None = Depends(authorize)) -> dict[str, object]:
        return service.printing_view()

    @app.get("/desktop/printing/jobs")
    async def get_print_jobs(limit: int = 50, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.list_print_jobs(limit=limit)

    # -- Memory ----------------------------------------------------------
    @app.get("/desktop/memory")
    async def get_memory(_: None = Depends(authorize)) -> dict[str, object]:
        return await service.list_memory()

    # -- Diagnostics -----------------------------------------------------
    @app.get("/desktop/diagnostics")
    async def get_diagnostics(_: None = Depends(authorize)) -> dict[str, object]:
        return service.diagnostics()

    @app.get("/desktop/diagnostics/export")
    async def export_diagnostics(_: None = Depends(authorize)) -> dict[str, object]:
        return service.safe_export()

    return app


__all__ = ["create_desktop_app"]
