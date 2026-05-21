"""add project_page_id to sync_tasks (Projects-DB status dot)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-05-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("sync_tasks", sa.Column("project_page_id", sa.String(length=64), nullable=True))
    op.create_index("ix_sync_tasks_project", "sync_tasks", ["project_page_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_sync_tasks_project", table_name="sync_tasks")
    op.drop_column("sync_tasks", "project_page_id")
