"""phase04 local speech action metadata

Revision ID: 7d4f1a2b8c93
Revises: 2c4d9e7a1b30
Create Date: 2026-10-08 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from arabic_agent.local.persistence.schema import TZDateTime

revision: str = "7d4f1a2b8c93"
down_revision: str | None = "2c4d9e7a1b30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "local_speech_actions",
        sa.Column("id", sa.String(length=200), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("run_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("engine", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=16), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", TZDateTime(length=40), nullable=False),
        sa.Column("updated_at", TZDateTime(length=40), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_local_speech_actions")),
    )
    op.create_index(
        op.f("ix_local_speech_actions_tenant_id"),
        "local_speech_actions",
        ["tenant_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_local_speech_actions_run_id"),
        "local_speech_actions",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_local_speech_actions_kind"),
        "local_speech_actions",
        ["kind"],
        unique=False,
    )
    op.create_index(
        "ix_local_speech_actions_tenant_run",
        "local_speech_actions",
        ["tenant_id", "run_id"],
        unique=False,
    )
    op.create_index(
        "ix_local_speech_actions_tenant_created",
        "local_speech_actions",
        ["tenant_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_local_speech_actions_tenant_created", table_name="local_speech_actions")
    op.drop_index("ix_local_speech_actions_tenant_run", table_name="local_speech_actions")
    op.drop_index(op.f("ix_local_speech_actions_kind"), table_name="local_speech_actions")
    op.drop_index(op.f("ix_local_speech_actions_run_id"), table_name="local_speech_actions")
    op.drop_index(op.f("ix_local_speech_actions_tenant_id"), table_name="local_speech_actions")
    op.drop_table("local_speech_actions")
