"""add task_folders table + task_folder_sync task_type

Creates the `task_folders` cache (Notion task page → NAS folders), extends the
sync_tasks task_type CHECK to include 'task_folder_sync', and adds a partial
unique index giving "one active task_folder_sync per task" (gmail_thread_id
carries the task page id, mirroring the label_sync trick on project_page_id).

Revision ID: f1a2b3c4d5e6
Revises: e9f7a8b6c530
Create Date: 2026-05-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e9f7a8b6c530"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "task_folders",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("project_page_id", sa.String(length=64), nullable=False),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column("current_discipline", sa.String(length=64), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_task_folders_project_page_id",
        "task_folders",
        ["project_page_id"],
    )

    # Extend the sync_tasks task_type CHECK. Postgres doesn't support ALTER
    # CONSTRAINT on a CHECK, so drop+recreate with the new allow-list.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','task_folder_sync')",
    )

    # One active task_folder_sync per task. gmail_thread_id carries the task
    # page id (placeholder), same dedup trick label_sync uses on project_page_id.
    op.create_index(
        "uq_sync_tasks_active_task_folder",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'task_folder_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_task_folder", table_name="sync_tasks")
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync')",
    )
    op.drop_index("ix_task_folders_project_page_id", table_name="task_folders")
    op.drop_table("task_folders")
