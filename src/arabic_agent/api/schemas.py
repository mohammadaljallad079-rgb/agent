"""Request and response schemas for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import (
    ApprovalStatus,
    ErrorClass,
    MisfirePolicy,
    RunStatus,
    StepKind,
    StepRunStatus,
    StepStatus,
    TriggerKind,
    WorkflowRunStatus,
    WorkflowStatus,
)
from ..specialists.base import GENERAL


class WorkflowStepRequest(BaseModel):
    """One step in a workflow definition submitted over HTTP."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)
    kind: StepKind
    depends_on: list[str] = Field(default_factory=list)
    condition: str | None = None
    action: str | None = None
    tool: str | None = None
    goal: str | None = None
    prompt: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    retry: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=86_400.0)
    error_policy: str = "fail_run"


class WorkflowRequest(BaseModel):
    """Create or update a workflow, optionally publishing a new version."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    steps: list[WorkflowStepRequest] = Field(default_factory=list)
    default_timeout_seconds: float = Field(default=600.0, gt=0.0)
    run_timeout_seconds: float | None = Field(default=None, gt=0.0)
    publish: bool = False


class WorkflowSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    status: WorkflowStatus
    latest_version_id: str | None = None
    latest_version: int = 0
    created_at: str
    updated_at: str


class WorkflowVersionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workflow_id: str
    version: int
    steps: list[dict[str, Any]] = Field(default_factory=list)
    default_timeout_seconds: float
    run_timeout_seconds: float | None = None
    created_at: str


class WorkflowRunRequest(BaseModel):
    """Start a workflow run."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    workspace_dir: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=200)


class WorkflowStepRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    step_id: str
    index: int
    name: str
    kind: StepKind
    status: StepRunStatus
    attempts: int
    output: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    error_class: ErrorClass | None = None
    agent_run_id: str | None = None
    approval_id: str | None = None


class WorkflowRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    workflow_id: str
    workflow_version_id: str
    workflow_version: int
    status: WorkflowRunStatus
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    pending_approval_id: str | None = None
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None


class TriggerRequest(BaseModel):
    """Create a trigger for a workflow."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    workflow_id: str = Field(min_length=1, max_length=64)
    kind: TriggerKind
    cron: str | None = Field(default=None, max_length=200)
    timezone: str = Field(default="UTC", max_length=64)
    misfire_policy: MisfirePolicy = MisfirePolicy.SKIP
    event_type: str | None = Field(default=None, max_length=120)
    inputs: dict[str, Any] = Field(default_factory=dict)


class TriggerUpdateRequest(BaseModel):
    """Update the mutable fields of a trigger."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    cron: str | None = Field(default=None, max_length=200)
    timezone: str | None = Field(default=None, max_length=64)
    misfire_policy: MisfirePolicy | None = None
    event_type: str | None = Field(default=None, max_length=120)
    inputs: dict[str, Any] | None = None


class TriggerSummary(BaseModel):
    """A trigger as returned over HTTP (never includes the webhook secret)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    kind: TriggerKind
    workflow_id: str
    enabled: bool
    cron: str | None = None
    timezone: str
    misfire_policy: MisfirePolicy
    next_fire_at: str | None = None
    webhook_id: str | None = None
    event_type: str | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    last_fire_at: str | None = None
    last_run_id: str | None = None
    fire_count: int = 0
    last_error: str | None = None
    created_at: str
    updated_at: str


class TriggerCreatedResponse(BaseModel):
    """A newly created trigger plus its one-time webhook signing secret."""

    model_config = ConfigDict(extra="forbid")

    trigger: TriggerSummary
    #: Returned exactly once, only for webhook triggers; never on reads.
    webhook_secret: str | None = None


class TriggerFireRequest(BaseModel):
    """Fire a trigger manually."""

    model_config = ConfigDict(extra="forbid")

    inputs: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=200)


class TriggerFiringSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    trigger_id: str
    workflow_id: str
    kind: TriggerKind
    workflow_run_id: str | None = None
    scheduled_for: str | None = None
    detail: str = ""
    created_at: str


class DomainEventRequest(BaseModel):
    """Publish an internal domain event to fire matching event triggers."""

    model_config = ConfigDict(extra="forbid")

    event_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    event_id: str | None = Field(default=None, max_length=200)


class RunRequest(BaseModel):
    """Start an agent run for a goal."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=8000)
    workspace_dir: str | None = None
    #: Optional key making run creation idempotent across retries.
    idempotency_key: str | None = Field(default=None, max_length=200)
    #: Specialist profile to run under (Phase 04). Defaults to the general agent.
    specialist: str = Field(default=GENERAL, max_length=64)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    result: str = ""
    verified: bool = False
    pending_approval_id: str | None = None
    error: str | None = None
    specialist: str = GENERAL
    steps_executed: int = 0
    tool_calls: int = 0
    retries: int = 0
    replans: int = 0
    usage: dict[str, int] = Field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    finished_at: str | None = None


class RunSummary(BaseModel):
    """Compact run representation for list endpoints."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    goal: str
    specialist: str = GENERAL
    created_at: str
    updated_at: str


class ApprovalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    tool_name: str
    permission: str
    summary: str
    status: ApprovalStatus
    created_at: str
    expires_at: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Structured, read-only description of the operation's concrete effect,
    #: shown to the approver so the decision is informed rather than a blind
    #: yes/no on a tool name. Non-secret by construction (redacted at creation).
    preview: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    reason: str | None = None


class ToolSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    permissions: list[str]
    risks: dict[str, str]
    side_effecting: bool = False


class StepSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    index: int
    description: str
    status: StepStatus
    attempts: int
    result_summary: str | None = None


class TaskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    run_id: str
    goal: str
    revision: int
    steps: list[StepSummary]


class MemorySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: str
    key: str
    content: str
    importance: float
    updated_at: str


class AuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    run_id: str
    sequence: int
    message: str
    timestamp: str
    data: dict[str, Any] = Field(default_factory=dict)


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    service: str = "arabic-agent"
    version: str = ""
    environment: str
    runtime: str
    default_model: str
    tools: list[str]
    integrations: list[str]
    auth_enabled: bool
    execution_mode: str = "inline"
    durable: bool = False
    distributed: bool = False


class ReadinessProbeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    ok: bool
    critical: bool
    detail: str = ""
    duration_ms: float = 0.0


class ReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ready: bool
    checks: list[ReadinessProbeResponse]


class SpecialistSummary(BaseModel):
    """A specialist profile as exposed over the API."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name_ar: str
    name_en: str
    description_ar: str
    description_en: str
    domains: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    tool_scope: list[str] = Field(default_factory=list)
    #: ``available`` | ``degraded`` | ``unavailable`` against the running tools.
    status: str = "available"
    missing_tools: list[str] = Field(default_factory=list)
