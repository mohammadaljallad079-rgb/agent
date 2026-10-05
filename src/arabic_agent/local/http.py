"""HTTP surface for the local control API.

Bound to loopback by default (``AAP_LOCAL_CONTROL_HOST=127.0.0.1``) and never
exposed publicly. Every request must present the control token in the
``X-Local-Token`` header; the surface is mounted separately from the cloud API
so a desktop client never reaches the multi-tenant cloud routes, and the cloud
API never exposes the local control routes.

Endpoints:

* ``GET  /local/status``       — runtime state, mode, device, capabilities
* ``GET  /local/capabilities`` — the local capability catalogue
* ``GET  /local/approvals``    — pending human approvals
* ``GET  /local/sync``         — queued/attempted sync actions
* ``POST /local/mode``         — switch online/offline/hybrid
* ``POST /local/lifecycle``    — start/run/pause/resume/stop
* ``POST /local/sync/flush``   — attempt to upload permitted sync actions
"""

from __future__ import annotations

from fastapi import Depends, FastAPI, Header, status
from pydantic import BaseModel, Field

from ..core.config import Settings
from ..core.enums import OperatingMode
from ..core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
    ValidationError,
)
from .control import LocalControlAPI


class ModeRequest(BaseModel):
    mode: OperatingMode


class LifecycleRequest(BaseModel):
    action: str = Field(description="One of: start, run, pause, resume, stop")


def create_local_app(
    control: LocalControlAPI,
    *,
    settings: Settings,
    tenant_id: str = "local",
    user_id: str = "local",
) -> FastAPI:
    """Build the loopback-only local control app."""
    app = FastAPI(
        title="Arabic AI Agent — Local Runtime Control",
        version="0.1.0",
        description="Secure loopback control surface for the local agent runtime.",
    )

    async def authorize(
        x_local_token: str | None = Header(default=None, alias="X-Local-Token"),
    ) -> None:
        control.authorize(x_local_token)

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

    @app.get("/local/status")
    async def get_status(_: None = Depends(authorize)) -> dict[str, object]:
        snapshot = await control.snapshot(tenant_id=tenant_id)
        return {
            "runtime": snapshot.runtime,
            "tools": snapshot.tools,
            "pending_approvals": snapshot.pending_approvals,
            "active_runs": snapshot.active_runs,
            "pending_sync": snapshot.pending_sync,
            "local_model": snapshot.local_model,
        }

    @app.get("/local/capabilities")
    async def get_capabilities(_: None = Depends(authorize)) -> list[dict[str, object]]:
        snapshot = await control.snapshot(tenant_id=tenant_id)
        return snapshot.capabilities

    @app.get("/local/approvals")
    async def get_approvals(_: None = Depends(authorize)) -> list[dict[str, object]]:
        return await control.pending_approvals(tenant_id=tenant_id)

    @app.get("/local/sync")
    async def get_sync(_: None = Depends(authorize)) -> list[dict[str, object]]:
        return await control.pending_sync(tenant_id=tenant_id)

    @app.post("/local/mode")
    async def set_mode(payload: ModeRequest, _: None = Depends(authorize)) -> dict[str, object]:
        return await control.set_mode(payload.mode)

    @app.post("/local/lifecycle")
    async def lifecycle(
        payload: LifecycleRequest, _: None = Depends(authorize)
    ) -> dict[str, object]:
        return await control.lifecycle(payload.action)

    @app.post("/local/sync/flush")
    async def flush_sync(_: None = Depends(authorize)) -> dict[str, object]:
        uploaded = await control.flush_sync(tenant_id=tenant_id)
        return {"uploaded": uploaded}

    return app
