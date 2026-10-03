"""Request and response schemas for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ApprovalStatus, RunStatus, StepStatus


class RunRequest(BaseModel):
    """Start an agent run for a goal."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=8000)
    workspace_dir: str | None = None
    resume: bool = False
    locale: str | None = None
    #: Optional key making run creation idempotent across retries.
    idempotency_key: str | None = Field(default=None, max_length=200)


class RunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    result: str = ""
    verified: bool = False
    pending_approval_id: str | None = None
    error: str | None = None
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
