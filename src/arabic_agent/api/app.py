"""FastAPI application.

Exposes the platform over HTTP: start/resume runs, list and resolve approvals,
inspect the tool catalog, and a health endpoint. Every endpoint is scoped to the
authenticated principal's tenant; there is no cross-tenant read path.

The default application is built lazily so importing this module (e.g. in tests
or tooling) never requires model credentials.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

from ..agent.base import AgentRunResult
from ..container import Platform
from ..core.config import Settings
from ..core.enums import ApprovalStatus, RunStatus
from ..core.errors import (
    ApprovalRequiredError,
    ConfigurationError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
    ValidationError,
)
from ..permissions.base import Principal
from .auth import ApiKeyAuth, build_principal_dependency
from .rate_limit import RateLimiter
from .run_registry import RunRecord, RunRegistry
from .schemas import (
    ApprovalDecision,
    ApprovalSummary,
    HealthResponse,
    RunRequest,
    RunResponse,
    ToolSummary,
)

logger = logging.getLogger(__name__)


def _run_response(result: AgentRunResult) -> RunResponse:
    return RunResponse(
        run_id=result.run_id,
        status=result.status,
        result=result.result,
        verified=result.verified,
        pending_approval_id=result.pending_approval_id,
        error=result.error,
        steps_executed=result.steps_executed,
        tool_calls=result.tool_calls,
        retries=result.retries,
        replans=result.replans,
        usage={
            "prompt_tokens": result.usage.prompt_tokens,
            "completion_tokens": result.usage.completion_tokens,
            "total_tokens": result.usage.total_tokens,
        },
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
    limiter = RateLimiter(rate_per_minute=settings.rate_limit_per_minute)
    runs = RunRegistry()

    app = FastAPI(
        title="Arabic AI Agent Platform",
        version="0.1.0",
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
        if not limiter.allow(key):
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(limiter.retry_after_seconds(key))},
            )
        return await call_next(request)

    @app.exception_handler(PlatformError)
    async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
        if isinstance(exc, (ValidationError,)):
            code = status.HTTP_422_UNPROCESSABLE_ENTITY
        elif isinstance(exc, PermissionDeniedError):
            code = status.HTTP_403_FORBIDDEN
        elif isinstance(exc, ApprovalRequiredError):
            code = status.HTTP_409_CONFLICT
        elif isinstance(exc, NotFoundError):
            code = status.HTTP_404_NOT_FOUND
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
                )
            )
        return summaries

    # --- Runs -----------------------------------------------------------
    @app.post("/runs", response_model=RunResponse)
    async def start_run(
        payload: RunRequest, principal: Principal = Depends(principal_dep)
    ) -> RunResponse:
        runner = platform.build_runner()
        result = await runner.run(
            principal=principal,
            goal=payload.goal,
            workspace_dir=payload.workspace_dir,
            resume=payload.resume,
        )
        workspace = result.workspace_dir or payload.workspace_dir or ""
        await runs.put(
            RunRecord(
                run_id=result.run_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                goal=payload.goal,
                workspace_dir=workspace,
                status=result.status,
                pending_approval_id=result.pending_approval_id,
                result=result.result,
            )
        )
        return _run_response(result)

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, principal: Principal = Depends(principal_dep)) -> RunResponse:
        record = await runs.get(run_id, tenant_id=principal.tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return RunResponse(
            run_id=record.run_id,
            status=record.status,
            result=record.result,
            pending_approval_id=record.pending_approval_id,
        )

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
                arguments=a.arguments,
            )
            for a in pending
        ]

    @app.post("/approvals/{approval_id}/decide", response_model=RunResponse)
    async def decide_approval(
        approval_id: str,
        payload: ApprovalDecision,
        principal: Principal = Depends(principal_dep),
    ) -> RunResponse:
        approval = await platform.approvals.get(approval_id, tenant_id=principal.tenant_id)
        record = await runs.get(approval.run_id, tenant_id=principal.tenant_id)
        new_status = ApprovalStatus.APPROVED if payload.approve else ApprovalStatus.DENIED
        resolved = await platform.approvals.resolve(
            approval_id,
            tenant_id=principal.tenant_id,
            status=new_status,
            resolved_by=principal.user_id,
            reason=payload.reason,
        )
        if not payload.approve:
            # Denied: report the terminal state without re-running.
            return RunResponse(
                run_id=resolved.run_id,
                status=RunStatus.CANCELLED,
                result="approval denied",
            )
        if record is None:
            raise HTTPException(status_code=404, detail="run not found for approval")
        runner = platform.build_runner()
        result = await runner.resume_after_approval(
            principal=principal,
            approval=resolved,
            run_id=resolved.run_id,
            workspace_dir=record.workspace_dir,
        )
        await runs.update(
            RunRecord(
                run_id=result.run_id,
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                goal=record.goal,
                workspace_dir=record.workspace_dir,
                status=result.status,
                pending_approval_id=result.pending_approval_id,
                result=result.result,
            )
        )
        return _run_response(result)

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
