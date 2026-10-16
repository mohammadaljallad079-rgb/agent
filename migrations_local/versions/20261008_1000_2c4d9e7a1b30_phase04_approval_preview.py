"""phase04 approval preview on local approvals

Revision ID: 2c4d9e7a1b30
Revises: 68f1faaef094
Create Date: 2026-10-08 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "2c4d9e7a1b30"
down_revision: str | None = "68f1faaef094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The structured, non-secret preview shown to a human before they approve a
    # tool call. Existing rows get an empty object (SQLite needs a server
    # default to add a NOT NULL column).
    op.add_column(
        "local_approvals",
        sa.Column(
            "preview",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("local_approvals", "preview")
