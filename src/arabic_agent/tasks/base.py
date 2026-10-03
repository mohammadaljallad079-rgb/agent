"""Task data contracts.

A task is a durable, resumable plan. Each step records its status and any error,
so a run interrupted by a crash, a denial or a pending approval can be resumed
without redoing completed work.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import ErrorClass, StepStatus
from ..core.ids import PREFIX_STEP, PREFIX_TASK, new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TaskStep(BaseModel):
    """One step of a plan: a goal, an optional tool, and its outcome."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_STEP))
    index: int
    description: str
    #: Suggested tool for this step (the agent may choose differently at runtime).
    tool_hint: str | None = None
    status: StepStatus = StepStatus.PENDING
    attempts: int = 0
    #: Redacted, human-readable result summary.
    result_summary: str | None = None
    #: Structured, redacted result payload.
    result_data: dict[str, Any] = Field(default_factory=dict)
    error_class: ErrorClass | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in (StepStatus.DONE, StepStatus.SKIPPED, StepStatus.FAILED)


class TaskPlan(BaseModel):
    """An ordered plan for achieving a goal."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_TASK))
    tenant_id: str
    user_id: str
    run_id: str
    goal: str
    steps: tuple[TaskStep, ...] = ()
    revision: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)

    def current_step(self) -> TaskStep | None:
        """The first step that is not yet terminal."""
        for step in self.steps:
            if not step.is_terminal:
                return step
        return None

    def is_complete(self) -> bool:
        return all(step.status is StepStatus.DONE for step in self.steps)

    def with_step(self, step: TaskStep) -> TaskPlan:
        steps = tuple(step if s.id == step.id else s for s in self.steps)
        return self.model_copy(update={"steps": steps, "updated_at": _utcnow()})

    def render(self) -> str:
        """Render the plan as compact text for the model context."""
        lines = [f"Plan revision {self.revision} ({len(self.steps)} steps):"]
        for step in self.steps:
            marker = {
                StepStatus.PENDING: "[ ]",
                StepStatus.IN_PROGRESS: "[~]",
                StepStatus.DONE: "[x]",
                StepStatus.FAILED: "[!]",
                StepStatus.SKIPPED: "[-]",
                StepStatus.BLOCKED: "[#]",
            }[step.status]
            suffix = f" — {step.result_summary}" if step.result_summary else ""
            hint = f" (tool: {step.tool_hint})" if step.tool_hint else ""
            lines.append(f"{marker} {step.index + 1}. {step.description}{hint}{suffix}")
        return "\n".join(lines)

    @classmethod
    def from_descriptions(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        run_id: str,
        goal: str,
        descriptions: list[str],
        tool_hints: list[str | None] | None = None,
    ) -> TaskPlan:
        hints = tool_hints or [None] * len(descriptions)
        steps = tuple(
            TaskStep(index=i, description=desc, tool_hint=hints[i] if i < len(hints) else None)
            for i, desc in enumerate(descriptions)
        )
        return cls(
            tenant_id=tenant_id,
            user_id=user_id,
            run_id=run_id,
            goal=goal,
            steps=steps,
        )
