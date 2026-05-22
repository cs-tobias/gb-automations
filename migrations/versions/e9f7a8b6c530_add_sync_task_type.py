"""add task_type to sync_tasks + label_sync dedup index

Adds a `task_type` column ('thread' default | 'label_sync') so the durable queue
can carry label-sync work alongside thread sync. Scopes the existing active-thread
dedup index to task_type='thread' and adds a partial unique index giving "one
active label_sync per project".

Revision ID: e9f7a8b6c530
Revises: d4e5f6a7b8c9
Create Date: 2026-05-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f7a8b6c530"
down_revision: str | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sync_tasks",
        sa.Column("task_type", sa.String(length=16), server_default="thread", nullable=False),
    )
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync')",
    )

    # Re-scope the active-thread dedup to thread rows only, so label_sync rows
    # (which reuse user_email/gmail_thread_id as placeholders) can't collide.
    op.drop_index("uq_sync_tasks_active_thread", table_name="sync_tasks")
    op.create_index(
        "uq_sync_tasks_active_thread",
        "sync_tasks",
        ["user_email", "gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','in_progress') AND task_type = 'thread'"),
    )

    # One active label_sync per project (dedup for fast/repeat button clicks).
    op.create_index(
        "uq_sync_tasks_active_label",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'label_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_label", table_name="sync_tasks")
    op.drop_index("uq_sync_tasks_active_thread", table_name="sync_tasks")
    op.create_index(
        "uq_sync_tasks_active_thread",
        "sync_tasks",
        ["user_email", "gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','in_progress')"),
    )
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.drop_column("sync_tasks", "task_type")
