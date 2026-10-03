"""FastAPI application.

Exposes the platform over HTTP: start runs, read runs, list and resolve
approvals, inspect the tool catalog, the task plan and the audit trail, and a
health endpoint. Every endpoint is scoped to the authenticated principal's
tenant; there is no cross-tenant read path.

Two execution modes share this surface:

* ``inline`` — the request executes the run and returns its result (local
  runtime, tests). Latency equals task duration.
* ``queued`` — the request persists the run, enqueues a job and returns
  immediately; a worker executes it. This is the production shape, and it is
  what makes a killed request harmless: the run survives and is resumed.

The default application is built lazily so importing this module (e.g. in tests
or tooling) never requires model credentials.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from ..container import Platform
from ..core.config import Settings
from ..core.enums import RunStatus
from ..core.errors import (
    ApprovalRequiredError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
    ValidationError,
)
from ..permissions.base import Principal
from ..runs.base import RunRecord
from .auth import ApiKeyAuth, build_principal_dependency
from .schemas import (
    ApprovalDecision,
    ApprovalSummary,
    AuditSummary,
    HealthResponse,
    MemorySummary,
    RunRequest,
    RunResponse,
    RunSummary,
    TaskSummary,
    ToolSummary,
)
from .service import RunService

logger = logging.getLogger(__name__)


def _run_response(record: RunRecord) -> RunResponse:
    return RunResponse(
        run_id=record.id,
        status=record.status,
        result=record.result,
        verified=record.verified,
        pending_approval_id=record.pending_approval_id,
        error=record.error,
        steps_executed=record.steps_executed,
        tool_calls=record.tool_calls,
        retries=record.retries,
        replans=record.replans,
        usage=dict(record.usage),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
    )


def create_app(
    platform: Platform,
    *,
    settings: Settings | None = None,
    auth: ApiKeyAuth | None = None,
) -> FastAPI:
    """Build the FastAPI app around a wired :class:`Platform`."""
    settings = settings or platform.settings
    auth = auth or ApiKeyAuth.from_settings(settings)
    principal_dep = build_principal_dependency(auth, settings)
    limiter = platform.rate_limiter
    service = RunService(platform, settings=settings)

    app = FastAPI(
        title="Arabic AI Agent Platform",
        version="0.2.0",
        description="Arabic-first Agent Operating Platform API.",
    )

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def rate_limit_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        key = request.headers.get("X-API-Key") or (
            request.client.host if request.client else "anon"
        )
        if not await limiter.allow(key):
            retry_after = await limiter.retry_after_seconds(key)
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
        if isinstance(exc, ValidationError):
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        elif isinstance(exc, PermissionDeniedError):
            code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, ApprovalRequiredError):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
        elif isinstance(exc, ConflictError):
            code = status.HTTP_409_CONFLICT
        else:
            code = status.HTTP_500_INTERNAL_SERVER_ERROR
        # Never leak internals in production responses.
        detail = exc.message if not settings.is_production else "request failed"
        return JSONResponse(
            status_code=code,
            content={"detail": detail, "type": type(exc).__name__},
        )

    # --- Health ---------------------------------------------------------
    @app.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            environment=settings.environment,
            runtime=platform.runtime.kind.value,
            default_model=platform.gateway.default_model,
            tools=platform.tools.names(),
            integrations=platform.integrations.names(),
            auth_enabled=auth.enabled,
            execution_mode=settings.execution_mode,
            durable=platform.durable,
            distributed=platform.distributed,
        )

    # --- Tools ----------------------------------------------------------
    @app.get("/tools", response_model=list[ToolSummary])
    async def list_tools(principal: Principal = Depends(principal_dep)) -> list[ToolSummary]:
        summaries: list[ToolSummary] = []
        for tool in platform.tools.all():
            risks = {p.value: r.value for p, r in tool.permission_risks().items()}
            summaries.append(
                ToolSummary(
                    name=tool.name,
                    description=tool.description,
                    permissions=[p.value for p in tool.permissions],
                    risks=risks,
                    side_effecting=tool.side_effecting,
                )
            )
        return summaries

    # --- Runs -----------------------------------------------------------
    @app.post("/runs", response_model=RunResponse)
    async def start_run(
        payload: RunRequest, principal: Principal = Depends(principal_dep)
    ) -> RunResponse:
        record = await service.start(
            principal=principal,
            goal=payload.goal,
            workspace_dir=payload.workspace_dir,
            idempotency_key=payload.idempotency_key,
        )
        return _run_response(record)

    @app.get("/runs", response_model=list[RunSummary])
    async def list_runs(
        principal: Principal = Depends(principal_dep),
        status_filter: RunStatus | None = Query(default=None, alias="status"),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[RunSummary]:
        records = await service.list(
            tenant_id=principal.tenant_id, status=status_filter, limit=limit, offset=offset
        )
        return [
            RunSummary(
                run_id=r.id,
                status=r.status,
                goal=r.goal,
                created_at=r.created_at.isoformat(),
                updated_at=r.updated_at.isoformat(),
            )
            for r in records
        ]

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, principal: Principal = Depends(principal_dep)) -> RunResponse:
        record = await service.get(run_id, tenant_id=principal.tenant_id)
        return _run_response(record)

    @app.get("/runs/{run_id}/plan", response_model=TaskSummary)
    async def get_plan(run_id: str, principal: Principal = Depends(principal_dep)) -> TaskSummary:
        plan = await platform.tasks.load_by_run(tenant_id=principal.tenant_id, run_id=run_id)
        if plan is None:
            raise HTTPException(status_code=404, detail="no plan for run")
        return TaskSummary(
            id=plan.id,
            run_id=plan.run_id,
            goal=plan.goal,
            revision=plan.revision,
            steps=[
                {
                    "id": s.id,
                    "index": s.index,
                    "description": s.description,
                    "status": s.status,
                    "attempts": s.attempts,
                    "result_summary": s.result_summary,
                }
                for s in plan.steps
            ],
        )

    @app.get("/runs/{run_id}/audit", response_model=list[AuditSummary])
    async def get_audit(
        run_id: str, principal: Principal = Depends(principal_dep)
    ) -> list[AuditSummary]:
        events = await platform.audit.history(tenant_id=principal.tenant_id, run_id=run_id)
        return [
            AuditSummary(
                id=e.id,
                type=e.type.value,
                run_id=e.run_id,
                sequence=e.sequence,
                message=e.message,
                timestamp=e.timestamp.isoformat(),
                data=dict(e.data),
            )
            for e in events
        ]

    # --- Approvals ------------------------------------------------------
    @app.get("/approvals", response_model=list[ApprovalSummary])
    async def list_approvals(
        principal: Principal = Depends(principal_dep),
    ) -> list[ApprovalSummary]:
        pending = await platform.approvals.list_pending(tenant_id=principal.tenant_id)
        return [
            ApprovalSummary(
                id=a.id,
                run_id=a.run_id,
                tool_name=a.tool_name,
                permission=a.permission.value,
                summary=a.summary,
                status=a.status,
                created_at=a.created_at.isoformat(),
                expires_at=a.expires_at.isoformat(),
                arguments=dict(a.arguments),
            )
            for a in pending
        ]

    @app.post("/approvals/{approval_id}/decide", response_model=RunResponse)
    async def decide_approval(
        approval_id: str,
        payload: ApprovalDecision,
        principal: Principal = Depends(principal_dep),
    ) -> RunResponse:
        record = await service.decide_approval(
            principal=principal,
            approval_id=approval_id,
            approve=payload.approve,
            reason=payload.reason,
        )
        return _run_response(record)

    # --- Memory ---------------------------------------------------------
    @app.get("/memory", response_model=list[MemorySummary])
    async def list_memory(
        principal: Principal = Depends(principal_dep),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> list[MemorySummary]:
        from ..memory.base import MemoryQuery

        entries = await platform.memory.query(
            MemoryQuery(tenant_id=principal.tenant_id, user_id=principal.user_id, limit=limit)
        )
        return [
            MemorySummary(
                id=m.id,
                kind=m.kind.value,
                key=m.key,
                content=m.content,
                importance=m.importance,
                updated_at=m.updated_at.isoformat(),
            )
            for m in entries
        ]

    return app


def build_default_app() -> FastAPI:
    """Build the app from environment configuration (used by ``uvicorn``)."""
    platform = Platform.build(Settings())
    return create_app(platform, settings=platform.settings)


def main() -> None:
    """Console entry point."""
    import sys

    import uvicorn

    settings = Settings()
    try:
        app = build_default_app()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="debug" if settings.debug else "info",
    )


__all__ = ["build_default_app", "create_app", "main"]
