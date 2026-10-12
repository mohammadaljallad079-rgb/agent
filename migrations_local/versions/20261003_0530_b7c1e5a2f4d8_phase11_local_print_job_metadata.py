"""phase11 local print job metadata

Revision ID: b7c1e5a2f4d8
Revises: 9faa23d73417
Create Date: 2026-10-03 05:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from arabic_agent.local.persistence.schema import JSONType, TZDateTime

revision: str = "b7c1e5a2f4d8"
down_revision: str | None = "9faa23d73417"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_print_jobs",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("printer_id", sa.String(length=200), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("backend", sa.String(length=32), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("options", JSONType, nullable=False),
        sa.Column("observed", sa.Boolean(), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", TZDateTime(length=40), nullable=False),
        sa.Column("updated_at", TZDateTime(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_local_print_jobs")),
    )
    op.create_index(
        op.f("ix_local_print_jobs_tenant_id"), "local_print_jobs", ["tenant_id"], unique=False
    )
    op.create_index(
        op.f("ix_local_print_jobs_run_id"), "local_print_jobs", ["run_id"], unique=False
    )
    op.create_index(
        op.f("ix_local_print_jobs_status"), "local_print_jobs", ["status"], unique=False
    )
    op.create_index(
        "ix_local_print_jobs_tenant_run", "local_print_jobs", ["tenant_id", "run_id"], unique=False
    )
    op.create_index(
        "ix_local_print_jobs_tenant_created",
        "local_print_jobs",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_local_print_jobs_tenant_created", table_name="local_print_jobs")
    op.drop_index("ix_local_print_jobs_tenant_run", table_name="local_print_jobs")
    op.drop_index(op.f("ix_local_print_jobs_status"), table_name="local_print_jobs")
    op.drop_index(op.f("ix_local_print_jobs_run_id"), table_name="local_print_jobs")
    op.drop_index(op.f("ix_local_print_jobs_tenant_id"), table_name="local_print_jobs")
    op.drop_table("local_print_jobs")
