"""SQLAlchemy ORM schema.

The schema mirrors the domain contracts (``runs.base.RunRecord``,
``tasks.base.TaskPlan``, ``permissions.approvals.Approval``,
``memory.base.MemoryEntry``, ``audit.base.AuditEvent``). Tables are normalised
where it helps querying and indexing (steps, approvals, audit), and use JSONB
for structured, non-queried payloads.

Tenant isolation is a first-class column on every table, indexed together with
the columns queries filter on, so a query can never scan across tenants.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

#: Deterministic constraint names keep Alembic migrations reproducible.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base with a stable constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class RunRow(Base):
    """Durable agent run."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    workspace_dir: Mapped[str] = mapped_column(Text, nullable=False, default="")
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    granted_permissions: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)

    result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pending_approval_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    steps_executed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replans: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # Idempotent run creation per tenant.
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_runs_tenant_idempotency"),
        # Tenant-scoped listing by status, newest first.
        Index("ix_runs_tenant_status_created", "tenant_id", "status", "created_at"),
        # Stale-run recovery scan.
        Index("ix_runs_status_lease", "status", "lease_expires_at"),
    )


class TaskPlanRow(Base):
    """Durable task plan (one per run)."""

    __tablename__ = "task_plans"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    steps: Mapped[list[TaskStepRow]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="TaskStepRow.index",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "run_id", name="uq_task_plans_tenant_run"),
        Index("ix_task_plans_tenant_run", "tenant_id", "run_id"),
    )


class TaskStepRow(Base):
    """One step of a task plan."""

    __tablename__ = "task_steps"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("task_plans.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    tool_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    error_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    plan: Mapped[TaskPlanRow] = relationship(back_populates="steps")

    __table_args__ = (
        UniqueConstraint("plan_id", "index", name="uq_task_steps_plan_index"),
        Index("ix_task_steps_tenant_plan", "tenant_id", "plan_id"),
    )


class ApprovalRow(Base):
    """Human-in-the-loop approval request."""

    __tablename__ = "approvals"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    permission: Mapped[str] = mapped_column(String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_approvals_tenant_status", "tenant_id", "status"),
        Index("ix_approvals_tenant_run", "tenant_id", "run_id"),
    )


class MemoryRow(Base):
    """Durable memory entry."""

    __tablename__ = "memory_entries"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    #: NULL means tenant-scoped (shared across the tenant).
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # One entry per (tenant, scope, kind, key): a re-write updates in place.
        UniqueConstraint("tenant_id", "user_id", "kind", "key", name="uq_memory_scope_kind_key"),
        Index("ix_memory_tenant_kind_importance", "tenant_id", "kind", "importance"),
        Index("ix_memory_expires", "expires_at"),
    )


class AuditEventRow(Base):
    """Durable audit event."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Ordering guarantee within a run.
        UniqueConstraint("run_id", "sequence", name="uq_audit_events_run_sequence"),
        Index("ix_audit_events_tenant_run_time", "tenant_id", "run_id", "timestamp"),
        Index("ix_audit_events_tenant_type_time", "tenant_id", "type", "timestamp"),
    )


__all__ = [
    "ApprovalRow",
    "AuditEventRow",
    "Base",
    "MemoryRow",
    "RunRow",
    "TaskPlanRow",
    "TaskStepRow",
]
