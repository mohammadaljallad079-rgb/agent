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
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import Response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from .. import __version__
from ..artifacts import list_artifacts, media_type_for, resolve_artifact, run_workspace
from ..audit.base import AuditEventType
from ..container import Platform
from ..core.config import Settings
from ..core.enums import OperatingMode, RunStatus, TriggerKind
from ..core.errors import (
    ApprovalRequiredError,
    ConfigurationError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    PlatformError,
    ValidationError,
)
from ..core.ids import PREFIX_REQUEST, PREFIX_WORKFLOW_RUN, new_id
from ..observability import configure_observability
from ..observability.context import correlation_scope
from ..observability.metrics import render_prometheus
from ..permissions.base import Principal
from ..runs.base import RunRecord
from ..triggers.events import DomainEvent
from ..triggers.models import Trigger, TriggerRun
from ..workflows.models import WorkflowStep
from .auth import ApiKeyAuth, build_principal_dependency
from .schemas import (
    ApprovalDecision,
    ApprovalSummary,
    ArtifactCollectionResponse,
    ArtifactSummary,
    AuditSummary,
    DomainEventRequest,
    HealthResponse,
    MemorySummary,
    ModelStatusResponse,
    ProviderStatus,
    ReadinessProbeResponse,
    ReadinessResponse,
    RunArtifactsResponse,
    RunRequest,
    RunResponse,
    RunSummary,
    SpecialistSummary,
    TaskSummary,
    ToolSummary,
    TriggerCreatedResponse,
    TriggerFireRequest,
    TriggerFiringSummary,
    TriggerRequest,
    TriggerSummary,
    TriggerUpdateRequest,
    VoiceStatusResponse,
    WorkflowRequest,
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowStepRequest,
    WorkflowStepRunSummary,
    WorkflowSummary,
    WorkflowVersionSummary,
)
from .service import RunService
from .workflow_service import WorkflowService

logger = logging.getLogger(__name__)


def new_request_id() -> str:
    """Generate a correlation id for an inbound request."""
    return new_id(PREFIX_REQUEST)


def _run_response(record: RunRecord) -> RunResponse:
    return RunResponse(
        run_id=record.id,
        status=record.status,
        result=record.result,
        verified=record.verified,
        pending_approval_id=record.pending_approval_id,
        error=record.error,
        specialist=record.specialist,
        steps_executed=record.steps_executed,
        tool_calls=record.tool_calls,
        retries=record.retries,
        replans=record.replans,
        usage=dict(record.usage),
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
    )


def _workflow_summary(record: object) -> WorkflowSummary:
    from ..workflows.models import Workflow

    assert isinstance(record, Workflow)
    return WorkflowSummary(
        id=record.id,
        name=record.name,
        description=record.description,
        status=record.status,
        latest_version_id=record.latest_version_id,
        latest_version=record.latest_version,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _workflow_version_summary(record: object) -> WorkflowVersionSummary:
    from ..workflows.models import WorkflowVersion

    assert isinstance(record, WorkflowVersion)
    return WorkflowVersionSummary(
        id=record.id,
        workflow_id=record.workflow_id,
        version=record.version,
        steps=[s.model_dump(mode="json") for s in record.steps],
        default_timeout_seconds=record.default_timeout_seconds,
        run_timeout_seconds=record.run_timeout_seconds,
        created_at=record.created_at.isoformat(),
    )


def _workflow_run_response(record: object) -> WorkflowRunResponse:
    from ..workflows.run import WorkflowRun

    assert isinstance(record, WorkflowRun)
    return WorkflowRunResponse(
        id=record.id,
        workflow_id=record.workflow_id,
        workflow_version_id=record.workflow_version_id,
        workflow_version=record.workflow_version,
        status=record.status,
        input=dict(record.input),
        output=dict(record.output),
        error=record.error,
        pending_approval_id=record.pending_approval_id,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
        started_at=record.started_at.isoformat() if record.started_at else None,
        finished_at=record.finished_at.isoformat() if record.finished_at else None,
    )


def _workflow_step_run_summary(record: object) -> WorkflowStepRunSummary:
    from ..workflows.run import StepRun

    assert isinstance(record, StepRun)
    return WorkflowStepRunSummary(
        id=record.id,
        step_id=record.step_id,
        index=record.index,
        name=record.name,
        kind=record.kind,
        status=record.status,
        attempts=record.attempts,
        output=dict(record.output),
        summary=record.summary,
        error=record.error,
        error_class=record.error_class,
        agent_run_id=record.agent_run_id,
        approval_id=record.approval_id,
    )


def _trigger_summary(record: object) -> TriggerSummary:
    assert isinstance(record, Trigger)
    return TriggerSummary(
        id=record.id,
        name=record.name,
        kind=record.kind,
        workflow_id=record.workflow_id,
        enabled=record.enabled,
        cron=record.cron,
        timezone=record.timezone,
        misfire_policy=record.misfire_policy,
        next_fire_at=record.next_fire_at.isoformat() if record.next_fire_at else None,
        webhook_id=record.webhook_id,
        event_type=record.event_type,
        inputs=dict(record.inputs),
        last_fire_at=record.last_fire_at.isoformat() if record.last_fire_at else None,
        last_run_id=record.last_run_id,
        fire_count=record.fire_count,
        last_error=record.last_error,
        created_at=record.created_at.isoformat(),
        updated_at=record.updated_at.isoformat(),
    )


def _trigger_firing_summary(record: TriggerRun) -> TriggerFiringSummary:
    return TriggerFiringSummary(
        id=record.id,
        trigger_id=record.trigger_id,
        workflow_id=record.workflow_id,
        kind=record.kind,
        workflow_run_id=record.workflow_run_id,
        scheduled_for=record.scheduled_for.isoformat() if record.scheduled_for else None,
        detail=record.detail,
        created_at=record.created_at.isoformat(),
    )


def _to_workflow_steps(steps: list[WorkflowStepRequest]) -> list[WorkflowStep]:
    from ..core.enums import StepErrorPolicy
    from ..workflows.models import RetryPolicy, WorkflowStep

    converted: list[WorkflowStep] = []
    for step in steps:
        retry = RetryPolicy(**step.retry) if step.retry else RetryPolicy()
        converted.append(
            WorkflowStep(
                id=step.id,
                name=step.name,
                kind=step.kind,
                depends_on=tuple(step.depends_on),
                condition=step.condition,
                action=step.action,
                tool=step.tool,
                goal=step.goal,
                prompt=step.prompt,
                arguments=dict(step.arguments),
                output=dict(step.output),
                retry=retry,
                timeout_seconds=step.timeout_seconds,
                error_policy=StepErrorPolicy(step.error_policy),
            )
        )
    return converted


def _lifespan(platform: Platform) -> Any:
    """Load the configured local model once at server start, then release it.

    A server entry point must do this itself: the local model backend loads its
    weights lazily on first inference, so without a startup load ``/models``
    would report the configured model as unavailable until the first request —
    and a first request would then pay the multi-second load inside a run's
    deadline. The load is non-fatal (``Platform.initialize`` marks the provider
    unavailable instead of raising), so a bad model path degrades to an honest
    "not loaded" status rather than a crash loop.
    """

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        await platform.initialize()
        try:
            yield
        finally:
            await platform.aclose()

    return lifespan


async def _provider_usability(provider: object) -> tuple[bool, str]:
    """Whether a provider can actually serve a request, and why not.

    A remote provider is usable when it carries a credential; a local provider
    is usable when its engine reports healthy. This is a gate, not a benchmark:
    it never makes a network call beyond the provider's own cheap probe, and it
    never raises — an unreachable dependency is reported, not propagated.
    """
    from ..local.model import LocalModelProvider
    from ..models.providers.setup_required import SetupRequiredProvider

    if isinstance(provider, SetupRequiredProvider):
        return False, "no model is configured yet"
    if isinstance(provider, LocalModelProvider):
        probe = getattr(provider, "probe", None)
        if probe is None:
            return False, "local engine does not report readiness"
        try:
            usable = bool(await probe())
        except Exception:  # a status probe must never fail the request
            return False, "local engine probe failed"
        return (usable, "" if usable else "local engine is not loaded")
    api_key = getattr(provider, "_api_key", None)
    if api_key is None:
        return True, ""
    return (bool(api_key), "" if api_key else "no API key configured")


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
    workflow_service = WorkflowService(platform, settings=settings)
    trigger_service = platform.trigger_service

    app = FastAPI(
        title="Super AI Agent Platform",
        version="0.2.0",
        description="Arabic-first Agent Operating Platform API.",
        lifespan=_lifespan(platform),
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
            platform.metrics.rate_limit_denied(scope="api")
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={"detail": "rate limit exceeded"},
                headers={"Retry-After": str(retry_after)},
            )
        return await call_next(request)

    @app.middleware("http")
    async def correlation_middleware(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Outermost middleware: every downstream log line, metric and span
        # inherits the request id. Honour an inbound id so a request can be
        # traced across an edge/proxy, but never trust it as identity.
        request_id = request.headers.get("X-Request-ID") or new_request_id()
        tenant_id = None
        api_key = request.headers.get("X-API-Key")
        if api_key:
            principal = auth.resolve(api_key)
            if principal is not None:
                tenant_id = principal.tenant_id
        with correlation_scope(request_id=request_id, tenant_id=tenant_id):
            response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

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
            service=settings.otel_service_name,
            version=__version__,
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

    @app.get("/readyz", response_model=ReadinessResponse)
    async def readyz() -> JSONResponse:
        """Readiness: can this process serve traffic right now?

        Returns 503 when a critical dependency (Postgres, Redis, the model
        route) is unreachable, so an orchestrator routes traffic elsewhere.
        Liveness (``/health``) stays 200 as long as the process is up.
        """
        report = await platform.readiness.check()
        body = ReadinessResponse(
            ready=report.ready,
            checks=[
                ReadinessProbeResponse(
                    name=r.name,
                    ok=r.ok,
                    critical=r.critical,
                    detail=r.detail,
                    duration_ms=r.duration_ms,
                )
                for r in report.results
            ],
        )
        for result in report.results:
            platform.metrics.dependency_up(dependency=result.name, up=result.ok)
        code = status.HTTP_200_OK if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE
        return JSONResponse(status_code=code, content=body.model_dump())

    @app.get("/metrics")
    async def metrics() -> Response:
        """Prometheus text exposition of the platform's metrics."""
        if not settings.metrics_enabled:
            raise HTTPException(status_code=404, detail="metrics disabled")
        body = render_prometheus(platform.metrics.registry)
        return Response(content=body, media_type="text/plain; version=0.0.4; charset=utf-8")

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

    # --- Specialists ----------------------------------------------------
    @app.get("/specialists", response_model=list[SpecialistSummary])
    async def list_specialists(
        principal: Principal = Depends(principal_dep),
    ) -> list[SpecialistSummary]:
        """Every specialist profile, with readiness against the running tools."""
        available = frozenset(platform.tools.names())
        readiness = {str(row["id"]): row for row in platform.specialists.readiness(available)}
        summaries: list[SpecialistSummary] = []
        for specialist in platform.specialists.all():
            row = readiness.get(specialist.id, {})
            scope = specialist.scoped_names(available)
            summaries.append(
                SpecialistSummary(
                    id=specialist.id,
                    name_ar=specialist.name_ar,
                    name_en=specialist.name_en,
                    description_ar=specialist.description_ar,
                    description_en=specialist.description_en,
                    domains=list(specialist.domains),
                    required_capabilities=sorted(c.value for c in specialist.required_capabilities),
                    tool_scope=sorted(scope),
                    status=str(row.get("status", "available")),
                    missing_tools=[str(m) for m in row.get("missing_tools", [])],
                )
            )
        return summaries

    @app.get("/specialists/{specialist_id}", response_model=SpecialistSummary)
    async def get_specialist(
        specialist_id: str, principal: Principal = Depends(principal_dep)
    ) -> SpecialistSummary:
        specialist = platform.specialists.get(specialist_id)
        available = frozenset(platform.tools.names())
        row = next(
            (r for r in platform.specialists.readiness(available) if r["id"] == specialist.id),
            {},
        )
        return SpecialistSummary(
            id=specialist.id,
            name_ar=specialist.name_ar,
            name_en=specialist.name_en,
            description_ar=specialist.description_ar,
            description_en=specialist.description_en,
            domains=list(specialist.domains),
            required_capabilities=sorted(c.value for c in specialist.required_capabilities),
            tool_scope=sorted(specialist.scoped_names(available)),
            status=str(row.get("status", "available")),
            missing_tools=[str(m) for m in row.get("missing_tools", [])],
        )

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
            specialist=payload.specialist,
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
                specialist=r.specialist,
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
                preview=dict(a.preview),
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
        # An approval may belong to an agent run or a workflow run; the id
        # prefix routes it to the right service. Both resolve the same store.
        if approval.run_id.startswith(f"{PREFIX_WORKFLOW_RUN}_"):
            workflow_run = await workflow_service.decide_approval(
                principal=principal,
                approval_id=approval_id,
                approve=payload.approve,
                reason=payload.reason,
            )
            return RunResponse(
                run_id=workflow_run.id,
                status=RunStatus(workflow_run.status.value),
                pending_approval_id=workflow_run.pending_approval_id,
                error=workflow_run.error,
                created_at=workflow_run.created_at.isoformat(),
                updated_at=workflow_run.updated_at.isoformat(),
                finished_at=(
                    workflow_run.finished_at.isoformat() if workflow_run.finished_at else None
                ),
            )
        record = await service.decide_approval(
            principal=principal,
            approval_id=approval_id,
            approve=payload.approve,
            reason=payload.reason,
        )
        return _run_response(record)

    # --- Model status ---------------------------------------------------
    @app.get("/models", response_model=ModelStatusResponse)
    async def model_status(principal: Principal = Depends(principal_dep)) -> ModelStatusResponse:
        """The live model routing state, reported from the running platform.

        ``usable`` per provider is a real gate: a remote provider without a key
        or a local provider whose engine is not loaded reports ``False`` with a
        reason, so the Command Center can say "not configured" rather than
        offering a model that would fail on the first request. The probe is
        cheap and never raises.
        """
        gateway = platform.gateway
        providers: list[ProviderStatus] = []
        for provider in gateway.registry.all():
            usable, detail = await _provider_usability(provider)
            providers.append(
                ProviderStatus(
                    name=provider.name,
                    is_local=bool(getattr(provider, "is_local", False)),
                    usable=usable,
                    detail=detail,
                    capabilities=sorted(c.value for c in provider.capabilities),
                    context_window=getattr(provider, "context_window", None),
                )
            )
        return ModelStatusResponse(
            default_model=gateway.default_model,
            fallbacks=list(gateway.fallbacks),
            local_only=platform.settings.local_mode is OperatingMode.OFFLINE,
            providers=providers,
        )

    @app.get("/voice/status", response_model=VoiceStatusResponse)
    async def voice_status(principal: Principal = Depends(principal_dep)) -> VoiceStatusResponse:
        """Whether the speech surface exists on this build.

        Speech is local-only and opt-in; on a cloud build the tools are not
        registered at all, so this reports the truth instead of offering a
        control that cannot work.
        """
        available = platform.tools.has("speech_to_text") and platform.tools.has("text_to_speech")
        if available:
            return VoiceStatusResponse(enabled=True, reason="")
        return VoiceStatusResponse(
            enabled=False,
            reason=(
                "speech is local-only and opt-in (AAP_RUNTIME_KIND=local, AAP_SPEECH_ENABLED=true)"
            ),
        )

    # --- Generated artifacts --------------------------------------------
    @app.get("/runs/{run_id}/artifacts", response_model=RunArtifactsResponse)
    async def run_artifacts(
        run_id: str, principal: Principal = Depends(principal_dep)
    ) -> RunArtifactsResponse:
        """Real files a run wrote into its sandboxed workspace, newest first."""
        record = await service.get(run_id, tenant_id=principal.tenant_id)
        root = run_workspace(record.workspace_dir)
        if root is None or not root.is_dir():
            return RunArtifactsResponse(run_id=run_id, artifacts=[], count=0)
        items = list_artifacts(root, run_id=run_id)
        return RunArtifactsResponse(
            run_id=run_id,
            artifacts=[ArtifactSummary(**item) for item in items],
            count=len(items),
        )

    @app.get("/artifacts", response_model=ArtifactCollectionResponse)
    async def list_all_artifacts(
        principal: Principal = Depends(principal_dep),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> ArtifactCollectionResponse:
        """Generated documents across the tenant's most recent runs."""
        records = await service.list(tenant_id=principal.tenant_id, limit=limit)
        collected: list[ArtifactSummary] = []
        for record in records:
            root = run_workspace(record.workspace_dir)
            if root is None or not root.is_dir():
                continue
            collected.extend(
                ArtifactSummary(**item) for item in list_artifacts(root, run_id=record.id)
            )
        collected.sort(key=lambda a: a.modified_at, reverse=True)
        return ArtifactCollectionResponse(artifacts=collected, count=len(collected))

    @app.get("/runs/{run_id}/artifacts/download")
    async def download_artifact(
        run_id: str,
        path: str = Query(min_length=1, max_length=1000),
        principal: Principal = Depends(principal_dep),
    ) -> Response:
        """Download one generated artifact.

        The path is resolved through the run's filesystem sandbox, so only a
        real file inside that run's workspace can be read — a ``..`` escape or
        an absolute path is refused. HTML/JSON are served as attachments with a
        non-rendering content type so a generated page cannot execute script in
        the API origin.
        """
        record = await service.get(run_id, tenant_id=principal.tenant_id)
        resolved = resolve_artifact(record.workspace_dir, path)
        await platform.audit.record(
            AuditEventType.ARTIFACT_DOWNLOADED,
            tenant_id=principal.tenant_id,
            user_id=principal.user_id,
            run_id=run_id,
            message="generated artifact downloaded",
            data={"path": path, "bytes": resolved.stat().st_size},
        )
        media_type = media_type_for(resolved)
        filename = resolved.name.replace('"', "")
        return FileResponse(
            resolved,
            media_type=media_type,
            filename=filename,
            headers={"X-Content-Type-Options": "nosniff"},
        )

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

    # --- Workflows ------------------------------------------------------
    @app.post("/workflows", response_model=WorkflowSummary, status_code=status.HTTP_201_CREATED)
    async def create_workflow(
        payload: WorkflowRequest, principal: Principal = Depends(principal_dep)
    ) -> WorkflowSummary:
        workflow, _ = await workflow_service.create(
            principal=principal,
            name=payload.name,
            description=payload.description,
            steps=_to_workflow_steps(payload.steps),
            default_timeout_seconds=payload.default_timeout_seconds,
            run_timeout_seconds=payload.run_timeout_seconds,
            publish=payload.publish,
        )
        return _workflow_summary(workflow)

    @app.get("/workflows", response_model=list[WorkflowSummary])
    async def list_workflows(
        principal: Principal = Depends(principal_dep),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[WorkflowSummary]:
        records = await workflow_service.list_workflows(
            tenant_id=principal.tenant_id, limit=limit, offset=offset
        )
        return [_workflow_summary(r) for r in records]

    @app.get("/workflows/{workflow_id}", response_model=WorkflowSummary)
    async def get_workflow(
        workflow_id: str, principal: Principal = Depends(principal_dep)
    ) -> WorkflowSummary:
        record = await workflow_service.get(workflow_id, tenant_id=principal.tenant_id)
        return _workflow_summary(record)

    @app.put("/workflows/{workflow_id}", response_model=WorkflowSummary)
    async def update_workflow(
        workflow_id: str,
        payload: WorkflowRequest,
        principal: Principal = Depends(principal_dep),
    ) -> WorkflowSummary:
        workflow, _ = await workflow_service.update(
            principal=principal,
            workflow_id=workflow_id,
            name=payload.name,
            description=payload.description,
            steps=_to_workflow_steps(payload.steps) if payload.steps else None,
            default_timeout_seconds=payload.default_timeout_seconds,
            run_timeout_seconds=payload.run_timeout_seconds,
            publish=payload.publish,
        )
        return _workflow_summary(workflow)

    @app.get("/workflows/{workflow_id}/versions", response_model=list[WorkflowVersionSummary])
    async def list_workflow_versions(
        workflow_id: str, principal: Principal = Depends(principal_dep)
    ) -> list[WorkflowVersionSummary]:
        versions = await workflow_service.list_versions(
            tenant_id=principal.tenant_id, workflow_id=workflow_id
        )
        return [_workflow_version_summary(v) for v in versions]

    @app.post(
        "/workflows/{workflow_id}/runs",
        response_model=WorkflowRunResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def start_workflow_run(
        workflow_id: str,
        payload: WorkflowRunRequest,
        principal: Principal = Depends(principal_dep),
    ) -> WorkflowRunResponse:
        run = await workflow_service.start_run(
            principal=principal,
            workflow_id=workflow_id,
            inputs=payload.inputs,
            workspace_dir=payload.workspace_dir,
            idempotency_key=payload.idempotency_key,
        )
        return _workflow_run_response(run)

    @app.get("/workflows/{workflow_id}/runs", response_model=list[WorkflowRunResponse])
    async def list_workflow_runs(
        workflow_id: str,
        principal: Principal = Depends(principal_dep),
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> list[WorkflowRunResponse]:
        runs = await workflow_service.list_runs(
            tenant_id=principal.tenant_id, workflow_id=workflow_id, limit=limit, offset=offset
        )
        return [_workflow_run_response(r) for r in runs]

    @app.get("/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
    async def get_workflow_run(
        run_id: str, principal: Principal = Depends(principal_dep)
    ) -> WorkflowRunResponse:
        run = await workflow_service.get_run(run_id, tenant_id=principal.tenant_id)
        return _workflow_run_response(run)

    @app.get("/workflow-runs/{run_id}/steps", response_model=list[WorkflowStepRunSummary])
    async def list_workflow_run_steps(
        run_id: str, principal: Principal = Depends(principal_dep)
    ) -> list[WorkflowStepRunSummary]:
        steps = await workflow_service.list_step_runs(
            tenant_id=principal.tenant_id, workflow_run_id=run_id
        )
        return [_workflow_step_run_summary(s) for s in steps]

    # --- Triggers -------------------------------------------------------
    @app.post("/triggers", response_model=TriggerCreatedResponse, status_code=201)
    async def create_trigger(
        body: TriggerRequest, principal: Principal = Depends(principal_dep)
    ) -> TriggerCreatedResponse:
        trigger, secret = await trigger_service.create(
            principal=principal,
            name=body.name,
            workflow_id=body.workflow_id,
            kind=body.kind,
            cron=body.cron,
            timezone=body.timezone,
            misfire_policy=body.misfire_policy,
            event_type=body.event_type,
            inputs=body.inputs,
        )
        return TriggerCreatedResponse(trigger=_trigger_summary(trigger), webhook_secret=secret)

    @app.get("/triggers", response_model=list[TriggerSummary])
    async def list_triggers(
        workflow_id: str | None = None,
        kind: TriggerKind | None = None,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
        principal: Principal = Depends(principal_dep),
    ) -> list[TriggerSummary]:
        records = await trigger_service.list_triggers(
            tenant_id=principal.tenant_id,
            kind=kind,
            workflow_id=workflow_id,
            limit=limit,
            offset=offset,
        )
        return [_trigger_summary(t) for t in records]

    @app.get("/triggers/{trigger_id}", response_model=TriggerSummary)
    async def get_trigger(
        trigger_id: str, principal: Principal = Depends(principal_dep)
    ) -> TriggerSummary:
        return _trigger_summary(
            await trigger_service.get(trigger_id, tenant_id=principal.tenant_id)
        )

    @app.patch("/triggers/{trigger_id}", response_model=TriggerSummary)
    async def update_trigger(
        trigger_id: str,
        body: TriggerUpdateRequest,
        principal: Principal = Depends(principal_dep),
    ) -> TriggerSummary:
        updated = await trigger_service.update(
            principal=principal,
            trigger_id=trigger_id,
            name=body.name,
            cron=body.cron,
            timezone=body.timezone,
            misfire_policy=body.misfire_policy,
            event_type=body.event_type,
            inputs=body.inputs,
        )
        return _trigger_summary(updated)

    @app.post("/triggers/{trigger_id}/enable", response_model=TriggerSummary)
    async def enable_trigger(
        trigger_id: str, principal: Principal = Depends(principal_dep)
    ) -> TriggerSummary:
        updated = await trigger_service.set_enabled(
            principal=principal, trigger_id=trigger_id, enabled=True
        )
        return _trigger_summary(updated)

    @app.post("/triggers/{trigger_id}/disable", response_model=TriggerSummary)
    async def disable_trigger(
        trigger_id: str, principal: Principal = Depends(principal_dep)
    ) -> TriggerSummary:
        updated = await trigger_service.set_enabled(
            principal=principal, trigger_id=trigger_id, enabled=False
        )
        return _trigger_summary(updated)

    @app.delete("/triggers/{trigger_id}", status_code=204)
    async def delete_trigger(
        trigger_id: str, principal: Principal = Depends(principal_dep)
    ) -> Response:
        await trigger_service.delete(principal=principal, trigger_id=trigger_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/triggers/{trigger_id}/fire", response_model=WorkflowRunResponse)
    async def fire_trigger(
        trigger_id: str,
        body: TriggerFireRequest,
        principal: Principal = Depends(principal_dep),
    ) -> WorkflowRunResponse:
        run = await trigger_service.fire_manual(
            principal=principal,
            trigger_id=trigger_id,
            inputs=body.inputs,
            idempotency_key=body.idempotency_key,
        )
        return _workflow_run_response(run)

    @app.get("/triggers/{trigger_id}/firings", response_model=list[TriggerFiringSummary])
    async def list_trigger_firings(
        trigger_id: str,
        limit: int = Query(default=50, ge=1, le=200),
        principal: Principal = Depends(principal_dep),
    ) -> list[TriggerFiringSummary]:
        firings = await trigger_service.list_firings(
            tenant_id=principal.tenant_id, trigger_id=trigger_id, limit=limit
        )
        return [_trigger_firing_summary(f) for f in firings]

    @app.post("/webhooks/{webhook_id}", response_model=WorkflowRunResponse)
    async def receive_webhook(
        webhook_id: str,
        request: Request,
        x_signature: str | None = Header(default=None, alias="X-Signature"),
        x_timestamp: str | None = Header(default=None, alias="X-Timestamp"),
        x_delivery_id: str | None = Header(default=None, alias="X-Delivery-ID"),
    ) -> WorkflowRunResponse:
        # Authenticated by HMAC signature over the raw body, not an API key.
        body = await request.body()
        if not x_signature or not x_timestamp:
            raise PermissionDeniedError("missing webhook signature")
        run = await trigger_service.fire_webhook(
            webhook_id=webhook_id,
            body=body,
            timestamp=x_timestamp,
            signature=x_signature,
            delivery_id=x_delivery_id,
        )
        return _workflow_run_response(run)

    @app.post("/events", response_model=list[WorkflowRunResponse])
    async def publish_event(
        body: DomainEventRequest, principal: Principal = Depends(principal_dep)
    ) -> list[WorkflowRunResponse]:
        # Internal event ingestion. Only the publishing tenant's triggers match.
        allowed = settings.allowed_event_types
        if allowed and body.event_type not in allowed:
            raise ValidationError(
                "event type not accepted",
                details={"event_type": body.event_type},
            )
        runs = await trigger_service.fire_event(
            event_type=body.event_type,
            tenant_id=principal.tenant_id,
            payload=body.payload,
            event_id=body.event_id,
        )
        # Also fan the event out to in-process subscribers (best-effort).
        await platform.events.publish(
            DomainEvent(
                type=body.event_type,
                tenant_id=principal.tenant_id,
                payload=body.payload,
            )
        )
        return [_workflow_run_response(r) for r in runs]

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
    configure_observability(settings)
    try:
        app = create_app(Platform.build(settings), settings=settings)
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
