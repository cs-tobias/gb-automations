"""add nas_folder_sync task_type + active dedup index

Splits the NAS folder provisioning out of label_sync into its own queue task
so each system (Gmail / NAS / Frame.io) has independent retry/failure surface
and its own per-system Notion button. Extends the sync_tasks task_type CHECK
to include 'nas_folder_sync' and adds the partial unique index that collapses
double-clicks for the new task type — same pattern as
uq_sync_tasks_active_label / uq_sync_tasks_active_frame_project.

Revision ID: i4f5g6h7c8d9
Revises: h3e4f5g6b7c8
Create Date: 2026-05-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "i4f5g6h7c8d9"
down_revision: str | None = "h3e4f5g6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_task_sync')",
    )

    op.create_index(
        "uq_sync_tasks_active_nas_folder",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'nas_folder_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_nas_folder", table_name="sync_tasks")
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','task_folder_sync',"
        "'frame_project_sync','frame_task_sync')",
    )
