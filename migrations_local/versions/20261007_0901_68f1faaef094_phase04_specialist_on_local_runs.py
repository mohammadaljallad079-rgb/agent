"""phase04 specialist on local runs

Revision ID: 68f1faaef094
Revises: b7c1e5a2f4d8
Create Date: 2026-10-07 09:01:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "68f1faaef094"
down_revision: str | None = "b7c1e5a2f4d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Desktop runs also carry the specialist profile so a reopened/durable run
    # reconstructs the same tool scope. SQLite needs a server default to add a
    # NOT NULL column to an existing table.
    op.add_column(
        "local_runs",
        sa.Column(
            "specialist",
            sa.String(length=64),
            nullable=False,
            server_default="general",
        ),
    )


def downgrade() -> None:
    op.drop_column("local_runs", "specialist")
