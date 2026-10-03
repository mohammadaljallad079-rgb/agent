"""Request and response schemas for the HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ApprovalStatus, RunStatus


class RunRequest(BaseModel):
    """Start an agent run for a goal."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=8000)
    workspace_dir: str | None = None
    resume: bool = False
    locale: str | None = None


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


class HealthResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    environment: str
    runtime: str
    default_model: str
    tools: list[str]
    integrations: list[str]
    auth_enabled: bool
