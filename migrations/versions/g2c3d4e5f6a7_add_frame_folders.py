"""add frame_project_folders + frame_task_folders + frame_* sync_task types

Schema for Phase 1 of the Frame.io integration (Notion → Frame mirror). Adds
two cache tables — one per project, one per task — and extends the sync_tasks
task_type CHECK to include `frame_project_sync` and `frame_task_sync`. The
two partial unique indexes enforce "one active frame_* task per project/task"
the same way `uq_sync_tasks_active_label` and `uq_sync_tasks_active_task_folder`
do for the NAS/Gmail flows.

Revision ID: g2c3d4e5f6a7
Revises: a7b8c9d0e1f2
Create Date: 2026-05-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g2c3d4e5f6a7"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "frame_project_folders",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("frame_folder_id", sa.String(length=64), nullable=False),
        sa.Column("frame_parent_id", sa.String(length=64), nullable=False),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column("current_year", sa.String(length=4), nullable=False),
        sa.Column("frame_url", sa.String(length=512), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "frame_task_folders",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("project_page_id", sa.String(length=64), nullable=False),
        sa.Column("frame_folder_id", sa.String(length=64), nullable=False),
        sa.Column(
            "frame_placeholder_file_id", sa.String(length=64), nullable=False
        ),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column("current_discipline", sa.String(length=64), nullable=False),
        sa.Column("frame_url", sa.String(length=512), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_frame_task_folders_project_page_id",
        "frame_task_folders",
        ["project_page_id"],
    )

    # Extend the sync_tasks task_type CHECK to admit the two new types.
    # Postgres doesn't support ALTER CONSTRAINT on a CHECK — drop + recreate.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','task_folder_sync',"
        "'frame_project_sync','frame_task_sync')",
    )

    # One active frame_project_sync per project — same dedup intent as
    # uq_sync_tasks_active_label (collapses double-clicks); scoped to the new
    # task_type so it doesn't conflict with the label_sync index on the same
    # project_page_id column.
    op.create_index(
        "uq_sync_tasks_active_frame_project",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_project_sync'"
        ),
    )

    # One active frame_task_sync per task. Mirrors uq_sync_tasks_active_task_folder:
    # gmail_thread_id carries the task page id as a placeholder so dedup is on
    # a single indexed column.
    op.create_index(
        "uq_sync_tasks_active_frame_task",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_task_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_frame_task", table_name="sync_tasks")
    op.drop_index("uq_sync_tasks_active_frame_project", table_name="sync_tasks")
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','task_folder_sync')",
    )
    op.drop_index(
        "ix_frame_task_folders_project_page_id", table_name="frame_task_folders"
    )
    op.drop_table("frame_task_folders")
    op.drop_table("frame_project_folders")
