"""initial sync_cursors table

Revision ID: 58f2093b4a11
Revises:
Create Date: 2026-05-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "58f2093b4a11"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_cursors",
        sa.Column("source", sa.String(length=64), primary_key=True),
        sa.Column("cursor_value", sa.String(length=256), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("sync_cursors")
