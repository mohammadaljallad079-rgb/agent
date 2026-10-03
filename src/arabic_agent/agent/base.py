"""Agent run result and recovery decisions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import RunStatus, StrEnum
from ..models.base import Usage
from ..tasks.base import TaskPlan


class RecoveryAction(StrEnum):
    """What the recovery policy decided to do after a failure."""

    RETRY = "retry"  # retry the same tool with the same arguments
    REPAIR = "repair"  # re-ask the model to fix the arguments
    REPLAN = "replan"  # discard remaining steps and plan again
    APPROVE = "approve"  # pause for human approval
    SKIP = "skip"  # skip the step and continue
    ABORT = "abort"  # stop the run and report failure


class AgentRunResult(BaseModel):
    """The outcome of a single agent run, returned to the caller."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: RunStatus
    result: str = ""
    verified: bool = False
    plan: TaskPlan | None = None
    pending_approval_id: str | None = None
    error: str | None = None
    workspace_dir: str = ""
    steps_executed: int = 0
    tool_calls: int = 0
    retries: int = 0
    replans: int = 0
    usage: Usage = Field(default_factory=Usage)

    @property
    def is_success(self) -> bool:
        return self.status is RunStatus.COMPLETED
