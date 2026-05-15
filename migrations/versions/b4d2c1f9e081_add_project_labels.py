"""add project_labels table

Revision ID: b4d2c1f9e081
Revises: a3185a1531dc
Create Date: 2026-05-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d2c1f9e081"
down_revision: str | None = "a3185a1531dc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_labels",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("user_email", sa.String(length=254), primary_key=True),
        sa.Column("gmail_label_id", sa.String(length=64), nullable=False),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("project_labels")
