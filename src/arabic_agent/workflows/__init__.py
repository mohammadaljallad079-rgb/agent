"""Workflow engine: durable, versioned, multi-step execution.

A workflow is a validated DAG of steps. Each step is one of four kinds —
deterministic (a registered pure action), agent (the existing agent runtime),
tool (the existing policy-gated tool executor) or approval (human-in-the-loop).
Runs and step runs are persisted and driven by the worker, so a workflow
survives restarts, failures and long approval waits.
"""

from __future__ import annotations

from .actions import ActionRegistry, DeterministicAction, default_actions
from .engine import WorkflowEngine
from .expressions import (
    Expression,
    ExpressionError,
    ExpressionEvaluationError,
    evaluate_condition,
    render,
)
from .models import (
    RetryPolicy,
    Workflow,
    WorkflowStep,
    WorkflowVersion,
)
from .run import (
    STEP_RUN_TRANSITIONS,
    SUCCESSFUL_STEP_RUN_STATUSES,
    TERMINAL_STEP_RUN_STATUSES,
    TERMINAL_WORKFLOW_RUN_STATUSES,
    WORKFLOW_RUN_TRANSITIONS,
    StepRun,
    WorkflowRun,
)
from .store import (
    InMemoryWorkflowRunStore,
    InMemoryWorkflowStore,
    InMemoryWorkflowVersionStore,
    WorkflowRunStore,
    WorkflowStore,
    WorkflowVersionStore,
)

__all__ = [
    "STEP_RUN_TRANSITIONS",
    "SUCCESSFUL_STEP_RUN_STATUSES",
    "TERMINAL_STEP_RUN_STATUSES",
    "TERMINAL_WORKFLOW_RUN_STATUSES",
    "WORKFLOW_RUN_TRANSITIONS",
    "ActionRegistry",
    "DeterministicAction",
    "Expression",
    "ExpressionError",
    "ExpressionEvaluationError",
    "InMemoryWorkflowRunStore",
    "InMemoryWorkflowStore",
    "InMemoryWorkflowVersionStore",
    "RetryPolicy",
    "StepRun",
    "Workflow",
    "WorkflowEngine",
    "WorkflowRun",
    "WorkflowRunStore",
    "WorkflowStep",
    "WorkflowStore",
    "WorkflowVersion",
    "WorkflowVersionStore",
    "default_actions",
    "evaluate_condition",
    "render",
]
