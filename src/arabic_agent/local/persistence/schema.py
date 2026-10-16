"""SQLAlchemy schema for the local durable store.

This is a deliberate *subset* of the cloud schema (``persistence.schema``): only
the state the local runtime actually needs. It is not a duplicate of every cloud
table — the cloud keeps memory, triggers, task plans and the job queue, which the
offline runtime either does not use or keeps in its own lighter form.

Portability
-----------
Two column types are made portable so the same schema is correct on SQLite (the
local default) and on PostgreSQL (used only to cross-check the stores):

* :class:`TZDateTime` stores an ISO-8601 UTC string. SQLite's native
  ``DATETIME`` drops the timezone, which would make the tz-aware comparisons the
  engine performs (``lease_expires_at <= now``) raise at run time. A fixed-width
  UTC string round-trips tz-aware datetimes and compares chronologically as
  text, on every platform.
* ``JSONType`` maps to ``JSONB`` on PostgreSQL and to SQLite's JSON storage
  otherwise, so structured payloads behave identically.

Constraint names use the same deterministic naming convention as the cloud
schema, so Alembic autogenerate stays reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ...persistence.schema import NAMING_CONVENTION

#: JSON payloads: JSONB on PostgreSQL, SQLite JSON elsewhere.
JSONType = sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


class TZDateTime(sa.TypeDecorator[datetime]):
    """A timezone-aware UTC datetime stored as a fixed-width ISO-8601 string.

    SQLite has no native timezone-aware type: ``sa.DateTime(timezone=True)``
    silently drops the offset and returns naive datetimes, which then break
    comparisons against ``datetime.now(UTC)``. Storing a normalised UTC string
    keeps every value tz-aware on read and, because the format is fixed width,
    ordering in SQL matches chronological order.
    """

    impl = sa.String(40)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: sa.Dialect) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def process_result_value(self, value: Any, dialect: sa.Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class LocalBase(DeclarativeBase):
    """Declarative base for the local store's own metadata."""

    metadata = sa.MetaData(naming_convention=NAMING_CONVENTION)


class LocalWorkflowRow(LocalBase):
    """Local workflow definition envelope."""

    __tablename__ = "local_workflows"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    latest_version_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    latest_version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "name", name="uq_local_workflows_tenant_name"),
    )


class LocalWorkflowVersionRow(LocalBase):
    """Immutable local workflow definition snapshot."""

    __tablename__ = "local_workflow_versions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    definition: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    default_timeout_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False, default=600.0)
    run_timeout_seconds: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "workflow_id", "version", name="uq_local_workflow_versions_workflow_version"
        ),
    )


class LocalWorkflowRunRow(LocalBase):
    """Durable local workflow execution state (lease/heartbeat/recovery)."""

    __tablename__ = "local_workflow_runs"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    workflow_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    workflow_version_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    workflow_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    workspace_dir: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    pending_approval_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    run_timeout_seconds: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    roles: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    granted_permissions: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    recovery_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    worker_id: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_local_workflow_runs_tenant_idempotency"
        ),
        sa.Index("ix_local_workflow_runs_status_lease", "status", "lease_expires_at"),
    )


class LocalWorkflowStepRunRow(LocalBase):
    """Durable local step execution state."""

    __tablename__ = "local_workflow_step_runs"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    workflow_run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    index: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    name: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    output: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(sa.String(32), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "workflow_run_id", "step_id", name="uq_local_workflow_step_runs_run_step"
        ),
    )


class LocalApprovalRow(LocalBase):
    """Durable local human-in-the-loop approval."""

    __tablename__ = "local_approvals"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    tool_name: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    permission: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    arguments: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    summary: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Structured, non-secret preview of the operation, shown to the approver.
    preview: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict, server_default=sa.text("'{}'")
    )
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(sa.Text, nullable=True)

    __table_args__ = (sa.Index("ix_local_approvals_tenant_status", "tenant_id", "status"),)


class LocalAuditEventRow(LocalBase):
    """Durable local audit event (metadata only; payloads already redacted)."""

    __tablename__ = "local_audit_events"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    type: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    message: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    timestamp: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint("run_id", "sequence", name="uq_local_audit_events_run_sequence"),
        sa.Index("ix_local_audit_events_tenant_run_time", "tenant_id", "run_id", "timestamp"),
    )


class LocalSyncActionRow(LocalBase):
    """Durable local offline synchronization queue entry."""

    __tablename__ = "local_sync_actions"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    device_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (sa.Index("ix_local_sync_actions_tenant_status", "tenant_id", "status"),)


class LocalIdempotencyRow(LocalBase):
    """Durable local idempotency claim (prevents a repeated side effect)."""

    __tablename__ = "local_idempotency"

    key: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    #: ``in_flight:<fingerprint>`` while claimed, ``done:<json>`` once complete.
    state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)


class LocalActionLedgerRow(LocalBase):
    """Durable local external-action ledger entry.

    Phase 10 makes the local action ledger durable: an external effect (an email
    sent, a record written, a webhook delivered) must not be repeated after an
    application or runtime restart. This row stores only safe metadata and a
    canonical digest — never the raw payload or any secret — mirroring the
    in-memory record exactly so the same ``ActionRecord`` round-trips.
    """

    __tablename__ = "local_action_ledger"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    connector: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    operation: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    risk: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    request_digest: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    approval_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    approved_digest: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    provider_request_id: Mapped[str] = mapped_column(sa.String(200), nullable=False, default="")
    verification_status: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    error: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.UniqueConstraint(
            "tenant_id", "idempotency_key", name="uq_local_action_ledger_tenant_idem"
        ),
        sa.Index("ix_local_action_ledger_tenant_run", "tenant_id", "run_id"),
    )


class LocalPrintJobRow(LocalBase):
    """Durable local print-job metadata.

    A print job is a physical side effect a user may want to review or cancel
    after an application restart. This row stores only safe metadata — printer,
    title, status, size and the job's options — never the document bytes or its
    path. It mirrors the in-memory ``PrintJobRecord`` so the same record
    round-trips.
    """

    __tablename__ = "local_print_jobs"

    id: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="", index=True)
    printer_id: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    backend: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    options: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    observed: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    detail: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.Index("ix_local_print_jobs_tenant_run", "tenant_id", "run_id"),
        sa.Index("ix_local_print_jobs_tenant_created", "tenant_id", "created_at"),
    )


class LocalSpeechActionRow(LocalBase):
    """Durable local speech-action metadata.

    A recording or a spoken reply is a physical side effect a user may want to
    review after an application restart. This row stores only safe metadata —
    kind, engine, language, duration, size and outcome — never the audio bytes.
    A transcript is stored only when the caller explicitly opted into keeping it
    (``keep_text``); even then it is the recognised text, never the audio. It
    mirrors the in-memory ``SpeechRecord`` so the same record round-trips.
    """

    __tablename__ = "local_speech_actions"

    id: Mapped[str] = mapped_column(sa.String(200), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="", index=True)
    kind: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    state: Mapped[str] = mapped_column(sa.String(32), nullable=False, default="ok")
    engine: Mapped[str] = mapped_column(sa.String(64), nullable=False, default="")
    language: Mapped[str] = mapped_column(sa.String(16), nullable=False, default="")
    duration_seconds: Mapped[float] = mapped_column(sa.Float, nullable=False, default=0.0)
    size_bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    error: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)

    __table_args__ = (
        sa.Index("ix_local_speech_actions_tenant_run", "tenant_id", "run_id"),
        sa.Index("ix_local_speech_actions_tenant_created", "tenant_id", "created_at"),
    )


class LocalRunRow(LocalBase):
    """Durable local agent run (desktop sessions/tasks).

    The cloud runtime persists runs in Postgres. The local/desktop runtime needs
    the same durability so a user can close the app and reopen a completed task,
    and so a crash mid-run is recoverable. Column names match the cloud
    ``runs`` table, so the shared ``persistence.mapping`` helpers round-trip the
    record unchanged.
    """

    __tablename__ = "local_runs"

    id: Mapped[str] = mapped_column(sa.String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    goal: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.String(32), nullable=False, index=True)
    workspace_dir: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    idempotency_key: Mapped[str | None] = mapped_column(sa.String(200), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    specialist: Mapped[str] = mapped_column(
        sa.String(64), nullable=False, default="general", server_default="general"
    )
    roles: Mapped[list[str]] = mapped_column(JSONType, nullable=False, default=list)
    granted_permissions: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    result: Mapped[str] = mapped_column(sa.Text, nullable=False, default="")
    verified: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    pending_approval_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)
    steps_executed: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    tool_calls: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    retries: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    replans: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    recovery_attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    usage: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=1)
    worker_id: Mapped[str | None] = mapped_column(sa.String(120), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(TZDateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TZDateTime, nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "idempotency_key", name="uq_local_runs_tenant_idem"),
        sa.Index("ix_local_runs_tenant_status", "tenant_id", "status"),
    )


__all__ = [
    "JSONType",
    "LocalActionLedgerRow",
    "LocalApprovalRow",
    "LocalAuditEventRow",
    "LocalBase",
    "LocalIdempotencyRow",
    "LocalRunRow",
    "LocalSyncActionRow",
    "LocalWorkflowRow",
    "LocalWorkflowRunRow",
    "LocalWorkflowStepRunRow",
    "LocalWorkflowVersionRow",
    "TZDateTime",
]
