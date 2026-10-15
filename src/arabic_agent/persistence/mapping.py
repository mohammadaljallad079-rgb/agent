"""Row <-> domain conversion.

Kept in one place so the stores stay focused on queries and the mapping is
testable in isolation. Domain objects are the pydantic contracts the rest of
the platform already uses; ORM rows never leak past a store.
"""

from __future__ import annotations

from typing import Any

from ..audit.base import AuditEvent, AuditEventType
from ..core.enums import (
    ApprovalStatus,
    ErrorClass,
    MemoryKind,
    MisfirePolicy,
    RunStatus,
    StepKind,
    StepRunStatus,
    StepStatus,
    TriggerKind,
    WorkflowRunStatus,
    WorkflowStatus,
)
from ..memory.base import MemoryEntry
from ..permissions.approvals import Approval
from ..permissions.base import Permission
from ..runs.base import RunRecord
from ..tasks.base import TaskPlan, TaskStep
from ..triggers.models import Trigger, TriggerRun
from ..workflows.models import Workflow, WorkflowStep, WorkflowVersion
from ..workflows.run import StepRun, WorkflowRun
from .schema import (
    ApprovalRow,
    AuditEventRow,
    MemoryRow,
    RunRow,
    TaskPlanRow,
    TaskStepRow,
    TriggerRow,
    TriggerRunRow,
    WorkflowRow,
    WorkflowRunRow,
    WorkflowStepRunRow,
    WorkflowVersionRow,
)


# --- Runs -------------------------------------------------------------------
def run_to_row(record: RunRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "goal": record.goal,
        "status": record.status.value,
        "workspace_dir": record.workspace_dir,
        "idempotency_key": record.idempotency_key,
        "parent_run_id": record.parent_run_id,
        "specialist": record.specialist,
        "roles": list(record.roles),
        "granted_permissions": (
            list(record.granted_permissions) if record.granted_permissions is not None else None
        ),
        "result": record.result,
        "verified": record.verified,
        "error": record.error,
        "pending_approval_id": record.pending_approval_id,
        "steps_executed": record.steps_executed,
        "tool_calls": record.tool_calls,
        "retries": record.retries,
        "replans": record.replans,
        "recovery_attempts": record.recovery_attempts,
        "usage": dict(record.usage),
        "worker_id": record.worker_id,
        "lease_expires_at": record.lease_expires_at,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
    }


def run_from_row(row: RunRow) -> RunRecord:
    return RunRecord(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        goal=row.goal,
        status=RunStatus(row.status),
        workspace_dir=row.workspace_dir or "",
        idempotency_key=row.idempotency_key,
        parent_run_id=row.parent_run_id,
        specialist=row.specialist,
        roles=tuple(row.roles or ()),
        granted_permissions=(
            tuple(row.granted_permissions) if row.granted_permissions is not None else None
        ),
        result=row.result or "",
        verified=row.verified,
        error=row.error,
        pending_approval_id=row.pending_approval_id,
        steps_executed=row.steps_executed,
        tool_calls=row.tool_calls,
        retries=row.retries,
        replans=row.replans,
        recovery_attempts=row.recovery_attempts,
        usage=dict(row.usage or {}),
        version=row.version,
        worker_id=row.worker_id,
        lease_expires_at=row.lease_expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


# --- Tasks ------------------------------------------------------------------
def step_to_row(plan_id: str, tenant_id: str, step: TaskStep) -> dict[str, Any]:
    return {
        "id": step.id,
        "plan_id": plan_id,
        "tenant_id": tenant_id,
        "index": step.index,
        "description": step.description,
        "tool_hint": step.tool_hint,
        "status": step.status.value,
        "attempts": step.attempts,
        "result_summary": step.result_summary,
        "result_data": dict(step.result_data),
        "error_class": step.error_class.value if step.error_class else None,
        "error_message": step.error_message,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def step_from_row(row: TaskStepRow) -> TaskStep:
    return TaskStep(
        id=row.id,
        index=row.index,
        description=row.description,
        tool_hint=row.tool_hint,
        status=StepStatus(row.status),
        attempts=row.attempts,
        result_summary=row.result_summary,
        result_data=dict(row.result_data or {}),
        error_class=ErrorClass(row.error_class) if row.error_class else None,
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def plan_to_row(plan: TaskPlan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "tenant_id": plan.tenant_id,
        "user_id": plan.user_id,
        "run_id": plan.run_id,
        "goal": plan.goal,
        "revision": plan.revision,
        "created_at": plan.created_at,
        "updated_at": plan.updated_at,
    }


def plan_from_row(row: TaskPlanRow) -> TaskPlan:
    return TaskPlan(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        run_id=row.run_id,
        goal=row.goal,
        steps=tuple(step_from_row(s) for s in row.steps),
        revision=row.revision,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# --- Approvals --------------------------------------------------------------
def approval_to_row(approval: Approval) -> dict[str, Any]:
    return {
        "id": approval.id,
        "tenant_id": approval.tenant_id,
        "user_id": approval.user_id,
        "run_id": approval.run_id,
        "tool_name": approval.tool_name,
        "permission": approval.permission.value,
        "arguments": dict(approval.arguments),
        "summary": approval.summary,
        "preview": dict(approval.preview),
        "status": approval.status.value,
        "created_at": approval.created_at,
        "expires_at": approval.expires_at,
        "resolved_at": approval.resolved_at,
        "resolved_by": approval.resolved_by,
        "decision_reason": approval.decision_reason,
    }


def approval_from_row(row: ApprovalRow) -> Approval:
    return Approval(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        run_id=row.run_id,
        tool_name=row.tool_name,
        permission=Permission(row.permission),
        arguments=dict(row.arguments or {}),
        summary=row.summary,
        preview=dict(getattr(row, "preview", None) or {}),
        status=ApprovalStatus(row.status),
        created_at=row.created_at,
        expires_at=row.expires_at,
        resolved_at=row.resolved_at,
        resolved_by=row.resolved_by,
        decision_reason=row.decision_reason,
    )


# --- Memory -----------------------------------------------------------------
def memory_to_row(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "id": entry.id,
        "tenant_id": entry.tenant_id,
        "user_id": entry.user_id,
        "run_id": entry.run_id,
        "kind": entry.kind.value,
        "key": entry.key,
        "content": entry.content,
        "attributes": dict(entry.attributes),
        "importance": entry.importance,
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
        "expires_at": entry.expires_at,
    }


def memory_from_row(row: MemoryRow) -> MemoryEntry:
    return MemoryEntry(
        id=row.id,
        kind=MemoryKind(row.kind),
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        run_id=row.run_id,
        key=row.key,
        content=row.content,
        attributes=dict(row.attributes or {}),
        importance=row.importance,
        created_at=row.created_at,
        updated_at=row.updated_at,
        expires_at=row.expires_at,
    )


# --- Workflows --------------------------------------------------------------
def workflow_to_row(workflow: Workflow) -> dict[str, Any]:
    return {
        "id": workflow.id,
        "tenant_id": workflow.tenant_id,
        "user_id": workflow.user_id,
        "name": workflow.name,
        "description": workflow.description,
        "status": workflow.status.value,
        "latest_version_id": workflow.latest_version_id,
        "latest_version": workflow.latest_version,
        "created_at": workflow.created_at,
        "updated_at": workflow.updated_at,
    }


def workflow_from_row(row: WorkflowRow) -> Workflow:
    return Workflow(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        name=row.name,
        description=row.description or "",
        status=WorkflowStatus(row.status),
        latest_version_id=row.latest_version_id,
        latest_version=row.latest_version,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def workflow_version_to_row(version: WorkflowVersion) -> dict[str, Any]:
    return {
        "id": version.id,
        "tenant_id": version.tenant_id,
        "workflow_id": version.workflow_id,
        "user_id": version.user_id,
        "version": version.version,
        "definition": {"steps": [step.model_dump(mode="json") for step in version.steps]},
        "default_timeout_seconds": version.default_timeout_seconds,
        "run_timeout_seconds": version.run_timeout_seconds,
        "created_at": version.created_at,
    }


def workflow_version_from_row(row: WorkflowVersionRow) -> WorkflowVersion:
    raw_steps = (row.definition or {}).get("steps", [])
    steps = tuple(WorkflowStep.model_validate(step) for step in raw_steps)
    return WorkflowVersion(
        id=row.id,
        workflow_id=row.workflow_id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        version=row.version,
        steps=steps,
        default_timeout_seconds=row.default_timeout_seconds,
        run_timeout_seconds=row.run_timeout_seconds,
        created_at=row.created_at,
    )


def workflow_run_to_row(run: WorkflowRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "user_id": run.user_id,
        "workflow_id": run.workflow_id,
        "workflow_version_id": run.workflow_version_id,
        "workflow_version": run.workflow_version,
        "status": run.status.value,
        "input": dict(run.input),
        "output": dict(run.output),
        "workspace_dir": run.workspace_dir,
        "error": run.error,
        "pending_approval_id": run.pending_approval_id,
        "idempotency_key": run.idempotency_key,
        "run_timeout_seconds": run.run_timeout_seconds,
        "roles": list(run.roles),
        "granted_permissions": (
            list(run.granted_permissions) if run.granted_permissions is not None else None
        ),
        "recovery_attempts": run.recovery_attempts,
        "worker_id": run.worker_id,
        "lease_expires_at": run.lease_expires_at,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
    }


def workflow_run_from_row(row: WorkflowRunRow) -> WorkflowRun:
    return WorkflowRun(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        workflow_id=row.workflow_id,
        workflow_version_id=row.workflow_version_id,
        workflow_version=row.workflow_version,
        status=WorkflowRunStatus(row.status),
        input=dict(row.input or {}),
        output=dict(row.output or {}),
        workspace_dir=row.workspace_dir or "",
        error=row.error,
        pending_approval_id=row.pending_approval_id,
        idempotency_key=row.idempotency_key,
        run_timeout_seconds=row.run_timeout_seconds,
        roles=tuple(row.roles or ()),
        granted_permissions=(
            tuple(row.granted_permissions) if row.granted_permissions is not None else None
        ),
        recovery_attempts=row.recovery_attempts,
        worker_id=row.worker_id,
        lease_expires_at=row.lease_expires_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


def step_run_to_row(step: StepRun) -> dict[str, Any]:
    return {
        "id": step.id,
        "tenant_id": step.tenant_id,
        "workflow_run_id": step.workflow_run_id,
        "step_id": step.step_id,
        "index": step.index,
        "name": step.name,
        "kind": step.kind.value,
        "status": step.status.value,
        "attempts": step.attempts,
        "output": dict(step.output),
        "summary": step.summary,
        "error": step.error,
        "error_class": step.error_class.value if step.error_class else None,
        "agent_run_id": step.agent_run_id,
        "approval_id": step.approval_id,
        "tool_name": step.tool_name,
        "idempotency_key": step.idempotency_key,
        "created_at": step.created_at,
        "updated_at": step.updated_at,
        "started_at": step.started_at,
        "finished_at": step.finished_at,
    }


def step_run_from_row(row: WorkflowStepRunRow) -> StepRun:
    return StepRun(
        id=row.id,
        tenant_id=row.tenant_id,
        workflow_run_id=row.workflow_run_id,
        step_id=row.step_id,
        index=row.index,
        name=row.name or "",
        kind=StepKind(row.kind),
        status=StepRunStatus(row.status),
        attempts=row.attempts,
        output=dict(row.output or {}),
        summary=row.summary or "",
        error=row.error,
        error_class=ErrorClass(row.error_class) if row.error_class else None,
        agent_run_id=row.agent_run_id,
        approval_id=row.approval_id,
        tool_name=row.tool_name,
        idempotency_key=row.idempotency_key,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


# --- Audit ------------------------------------------------------------------
def audit_to_row(event: AuditEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "tenant_id": event.tenant_id,
        "user_id": event.user_id,
        "run_id": event.run_id,
        "type": event.type.value,
        "sequence": event.sequence,
        "message": event.message,
        "data": dict(event.data),
        "timestamp": event.timestamp,
    }


def audit_from_row(row: AuditEventRow) -> AuditEvent:
    return AuditEvent(
        id=row.id,
        type=AuditEventType(row.type),
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        run_id=row.run_id,
        timestamp=row.timestamp,
        sequence=row.sequence,
        message=row.message or "",
        data=dict(row.data or {}),
    )


# --- Triggers ---------------------------------------------------------------
def trigger_to_row(trigger: Trigger) -> dict[str, Any]:
    return {
        "id": trigger.id,
        "tenant_id": trigger.tenant_id,
        "user_id": trigger.user_id,
        "name": trigger.name,
        "kind": trigger.kind.value,
        "workflow_id": trigger.workflow_id,
        "enabled": trigger.enabled,
        "cron": trigger.cron,
        "timezone": trigger.timezone,
        "misfire_policy": trigger.misfire_policy.value,
        "next_fire_at": trigger.next_fire_at,
        "webhook_id": trigger.webhook_id,
        "webhook_secret": trigger.webhook_secret,
        "event_type": trigger.event_type,
        "inputs": dict(trigger.inputs),
        "roles": list(trigger.roles),
        "granted_permissions": (
            list(trigger.granted_permissions) if trigger.granted_permissions is not None else None
        ),
        "last_fire_at": trigger.last_fire_at,
        "last_run_id": trigger.last_run_id,
        "fire_count": trigger.fire_count,
        "last_error": trigger.last_error,
        "lease_owner": trigger.lease_owner,
        "lease_expires_at": trigger.lease_expires_at,
        "created_at": trigger.created_at,
        "updated_at": trigger.updated_at,
    }


def trigger_from_row(row: TriggerRow) -> Trigger:
    return Trigger(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        name=row.name,
        kind=TriggerKind(row.kind),
        workflow_id=row.workflow_id,
        enabled=row.enabled,
        cron=row.cron,
        timezone=row.timezone or "UTC",
        misfire_policy=MisfirePolicy(row.misfire_policy),
        next_fire_at=row.next_fire_at,
        webhook_id=row.webhook_id,
        webhook_secret=row.webhook_secret,
        event_type=row.event_type,
        inputs=dict(row.inputs or {}),
        roles=tuple(row.roles or ()),
        granted_permissions=(
            tuple(row.granted_permissions) if row.granted_permissions is not None else None
        ),
        last_fire_at=row.last_fire_at,
        last_run_id=row.last_run_id,
        fire_count=row.fire_count,
        last_error=row.last_error,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        version=row.version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def trigger_run_to_row(run: TriggerRun) -> dict[str, Any]:
    return {
        "id": run.id,
        "tenant_id": run.tenant_id,
        "trigger_id": run.trigger_id,
        "workflow_id": run.workflow_id,
        "kind": run.kind.value,
        "workflow_run_id": run.workflow_run_id,
        "scheduled_for": run.scheduled_for,
        "idempotency_key": run.idempotency_key,
        "detail": run.detail,
        "created_at": run.created_at,
    }


def trigger_run_from_row(row: TriggerRunRow) -> TriggerRun:
    return TriggerRun(
        id=row.id,
        tenant_id=row.tenant_id,
        trigger_id=row.trigger_id,
        workflow_id=row.workflow_id,
        kind=TriggerKind(row.kind),
        workflow_run_id=row.workflow_run_id,
        scheduled_for=row.scheduled_for,
        idempotency_key=row.idempotency_key,
        detail=row.detail or "",
        created_at=row.created_at,
    )
