"""phase04 specialist on runs

Revision ID: a56abcef33d3
Revises: a10a3291e754
Create Date: 2026-10-07 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "a56abcef33d3"
down_revision: str | None = "a10a3291e754"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A run executes under one specialist profile. Existing rows are the general
    # agent, so the column backfills to 'general' via its server default.
    op.add_column(
        "runs",
        sa.Column(
            "specialist",
            sa.String(length=64),
            nullable=False,
            server_default="general",
        ),
    )


def downgrade() -> None:
    op.drop_column("runs", "specialist")
