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
    RunStatus,
    StepStatus,
)
from ..memory.base import MemoryEntry
from ..permissions.approvals import Approval
from ..permissions.base import Permission
from ..runs.base import RunRecord
from ..tasks.base import TaskPlan, TaskStep
from .schema import (
    ApprovalRow,
    AuditEventRow,
    MemoryRow,
    RunRow,
    TaskPlanRow,
    TaskStepRow,
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
