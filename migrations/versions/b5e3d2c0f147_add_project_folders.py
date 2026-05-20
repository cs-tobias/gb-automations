"""add project_folders table

Revision ID: b5e3d2c0f147
Revises: a9c7d8e6f436
Create Date: 2026-05-20

Notion project page ↔ folder on the office NAS, one row per project. Analogous
to project_labels but the host filesystem is single (not per-user), so the
project page ID alone is the key. Stores the last-written path/name so a Notion
rename can move the folder in place instead of orphaning it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5e3d2c0f147"
down_revision: str | None = "a9c7d8e6f436"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_folders",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("current_path", sa.String(length=1024), nullable=False),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("project_folders")
