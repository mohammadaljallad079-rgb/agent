"""Workflow definition model.

A *workflow* is a durable, versioned executable graph. The definition is
untrusted input authored by a user, so this module is deliberately strict: it
validates the graph (unique ids, resolvable dependencies, no cycles, every step
of a recognised kind) at construction time. A definition that fails validation
can never be published or run.

Versioning is immutable: publishing a workflow produces a new
:class:`WorkflowVersion` with a fresh id and a monotonically increasing
``version`` number. A run pins the version id it started with, so editing a
workflow never mutates work that is already in flight.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..core.enums import StepErrorPolicy, StepKind, WorkflowStatus
from ..core.errors import ValidationError
from ..core.ids import PREFIX_WORKFLOW, PREFIX_WORKFLOW_VERSION, new_id


def _utcnow() -> datetime:
    return datetime.now(UTC)


#: How many parallel branches a run may fan out at once.
MAX_PARALLELISM = 32
#: Upper bound on steps in one workflow, to bound validation/scheduling cost.
MAX_STEPS = 200


class RetryPolicy(BaseModel):
    """Per-step retry configuration, mapped onto the existing retry semantics."""

    model_config = ConfigDict(frozen=True)

    max_attempts: int = Field(default=1, ge=1, le=10)
    #: Base backoff in seconds; attempt *n* waits ``base * n`` (linear), matching
    #: the job queue's backoff convention.
    backoff_seconds: float = Field(default=0.0, ge=0.0, le=3600.0)
    #: Error classes that may be retried. Empty means "the default retryable set".
    retryable_error_classes: tuple[str, ...] = ()

    def allows(self, error_class: str) -> bool:
        if not self.retryable_error_classes:
            return True
        return error_class in self.retryable_error_classes


class WorkflowStep(BaseModel):
    """One node in the workflow graph."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=64)
    name: str = Field(default="", max_length=200)
    kind: StepKind
    #: Step ids that must complete before this one may run. This is what makes
    #: the graph a DAG; parallel branches are simply siblings sharing a parent.
    depends_on: tuple[str, ...] = ()
    #: Skip this step (and treat it as satisfied) unless the condition is true.
    condition: str | None = None

    # --- Kind-specific configuration -----------------------------------
    #: DETERMINISTIC: the name of a registered deterministic action.
    action: str | None = None
    #: TOOL: the name of a registered tool.
    tool: str | None = None
    #: AGENT: the goal handed to the agent runtime (templated).
    goal: str | None = None
    #: APPROVAL: the prompt shown to the human (templated).
    prompt: str | None = None

    #: Arguments/payload passed to the action/tool/agent (templated at run time).
    arguments: dict[str, Any] = Field(default_factory=dict)
    #: Structured output mapping: name -> expression producing the value.
    output: dict[str, Any] = Field(default_factory=dict)

    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    #: Wall-clock timeout for this step in seconds (None = run-level default).
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=86_400.0)
    #: Whether a failure here fails the run, skips the step, or is tolerated.
    error_policy: StepErrorPolicy = StepErrorPolicy.FAIL_RUN
    #: Marks a step whose tool/agent effect must be guarded against duplicates.
    idempotent: bool = True


class WorkflowVersion(BaseModel):
    """An immutable snapshot of a workflow definition."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_WORKFLOW_VERSION))
    workflow_id: str
    tenant_id: str
    user_id: str
    #: Monotonic, 1-based version number within a workflow.
    version: int = 1
    steps: tuple[WorkflowStep, ...] = ()
    #: Default step timeout for steps that do not set one.
    default_timeout_seconds: float = Field(default=600.0, gt=0.0)
    #: Wall-clock timeout for the whole run (None = unbounded).
    run_timeout_seconds: float | None = Field(default=None, gt=0.0)
    created_at: datetime = Field(default_factory=_utcnow)

    # --- Graph helpers -------------------------------------------------
    def step_by_id(self, step_id: str) -> WorkflowStep:
        for step in self.steps:
            if step.id == step_id:
                return step
        raise ValidationError(f"unknown step: {step_id}", details={"step_id": step_id})

    def step_ids(self) -> tuple[str, ...]:
        return tuple(s.id for s in self.steps)

    def validate_graph(self) -> None:
        """Validate the DAG. Raises :class:`ValidationError` on any problem."""
        if not self.steps:
            raise ValidationError("workflow has no steps")
        if len(self.steps) > MAX_STEPS:
            raise ValidationError(f"workflow has too many steps (>{MAX_STEPS})")

        ids = self.step_ids()
        if len(set(ids)) != len(ids):
            raise ValidationError("duplicate step id")

        known = set(ids)
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in known:
                    raise ValidationError(
                        f"step '{step.id}' depends on unknown step '{dep}'",
                        details={"step": step.id, "dependency": dep},
                    )
                if dep == step.id:
                    raise ValidationError(f"step '{step.id}' depends on itself")
            self._validate_kind(step)
            self._validate_output(step)

        self._detect_cycle(known)

    @staticmethod
    def _validate_kind(step: WorkflowStep) -> None:
        if step.kind is StepKind.TOOL and not step.tool:
            raise ValidationError(f"tool step '{step.id}' must set 'tool'")
        if step.kind is StepKind.DETERMINISTIC and not step.action:
            raise ValidationError(f"deterministic step '{step.id}' must set 'action'")
        if step.kind is StepKind.AGENT and not step.goal:
            raise ValidationError(f"agent step '{step.id}' must set 'goal'")
        if step.kind is StepKind.APPROVAL and not step.prompt:
            raise ValidationError(f"approval step '{step.id}' must set 'prompt'")
        if step.error_policy not in {
            StepErrorPolicy.FAIL_RUN,
            StepErrorPolicy.SKIP,
            StepErrorPolicy.CONTINUE,
        }:
            raise ValidationError(
                f"step '{step.id}' has an invalid error_policy: {step.error_policy}"
            )

    @staticmethod
    def _validate_output(step: WorkflowStep) -> None:
        # Output keys become names later steps reference; keep them identifier-ish
        # so a template cannot inject surprising names.
        for key in step.output:
            if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
                raise ValidationError(
                    f"step '{step.id}' has an invalid output key: {key!r}",
                    details={"step": step.id, "key": key},
                )

    def _detect_cycle(self, known: set[str]) -> None:
        # Iterative DFS with a colour map; avoids recursion limits on big graphs.
        adjacency = {s.id: s.depends_on for s in self.steps}
        WHITE, GREY, BLACK = 0, 1, 2
        colour = dict.fromkeys(known, WHITE)
        for start in known:
            if colour[start] != WHITE:
                continue
            stack: list[tuple[str, int]] = [(start, 0)]
            colour[start] = GREY
            while stack:
                node, index = stack[-1]
                deps = adjacency[node]
                if index < len(deps):
                    stack[-1] = (node, index + 1)
                    dep = deps[index]
                    if colour[dep] == GREY:
                        raise ValidationError(
                            "workflow graph contains a cycle",
                            details={"at": node, "dependency": dep},
                        )
                    if colour[dep] == WHITE:
                        colour[dep] = GREY
                        stack.append((dep, 0))
                else:
                    colour[node] = BLACK
                    stack.pop()


class Workflow(BaseModel):
    """A workflow definition's mutable envelope, pointing at its latest version."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: new_id(PREFIX_WORKFLOW))
    tenant_id: str
    user_id: str
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    status: WorkflowStatus = WorkflowStatus.DRAFT
    #: The version a new run should use (only set once published).
    latest_version_id: str | None = None
    latest_version: int = 0
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    version: int = 1

    @property
    def is_published(self) -> bool:
        return self.status is WorkflowStatus.PUBLISHED and self.latest_version_id is not None

    def can_transition_to(self, target: WorkflowStatus) -> bool:
        return target in _WORKFLOW_TRANSITIONS.get(self.status, frozenset())

    def transition(self, target: WorkflowStatus, **updates: Any) -> Workflow:
        if target is not self.status and not self.can_transition_to(target):
            raise ValidationError(
                f"invalid workflow transition {self.status.value} -> {target.value}"
            )
        return self.model_copy(update={"status": target, "updated_at": _utcnow(), **updates})


#: Allowed lifecycle transitions for a workflow definition.
_WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.DRAFT: frozenset({WorkflowStatus.PUBLISHED, WorkflowStatus.ARCHIVED}),
    WorkflowStatus.PUBLISHED: frozenset({WorkflowStatus.DRAFT, WorkflowStatus.ARCHIVED}),
    WorkflowStatus.ARCHIVED: frozenset(),
}
