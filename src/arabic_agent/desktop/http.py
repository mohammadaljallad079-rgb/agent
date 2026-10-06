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
* ``GET  /desktop/models``         — catalogue + installed state
* ``POST /desktop/models/{id}/install`` / ``GET .../progress`` / ``POST .../cancel``
* ``POST /desktop/models/{id}/verify`` / ``POST .../health`` / ``DELETE /desktop/models/{id}``
* ``GET  /desktop/modes`` / ``POST /desktop/modes``
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
            "runs": runs,
            "approvals": await service.list_approvals(),
            "permissions": service.permissions_view(),
            "settings": service.settings_view(),
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
        return {"models": service.list_models(), "recommendation": service.recommend_model()}

    @app.post("/desktop/models/{model_id}/install", status_code=status.HTTP_202_ACCEPTED)
    async def install_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.install_model(model_id)

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

    @app.delete("/desktop/models/{model_id}")
    async def remove_model(model_id: str, _: None = Depends(authorize)) -> dict[str, object]:
        return service.remove_model(model_id)

    # -- Modes / privacy / settings --------------------------------------
    @app.get("/desktop/modes")
    async def get_modes(_: None = Depends(authorize)) -> dict[str, object]:
        return service.modes()

    @app.post("/desktop/modes")
    async def set_mode(payload: ModeRequest, _: None = Depends(authorize)) -> dict[str, object]:
        return await service.set_mode(payload.mode)

    @app.get("/desktop/settings")
    async def get_settings(_: None = Depends(authorize)) -> dict[str, object]:
        return service.settings_view()

    @app.get("/desktop/permissions")
    async def get_permissions(_: None = Depends(authorize)) -> dict[str, object]:
        return service.permissions_view()

    # -- Diagnostics -----------------------------------------------------
    @app.get("/desktop/diagnostics")
    async def get_diagnostics(_: None = Depends(authorize)) -> dict[str, object]:
        return service.diagnostics()

    @app.get("/desktop/diagnostics/export")
    async def export_diagnostics(_: None = Depends(authorize)) -> dict[str, object]:
        return service.safe_export()

    return app


__all__ = ["create_desktop_app"]
