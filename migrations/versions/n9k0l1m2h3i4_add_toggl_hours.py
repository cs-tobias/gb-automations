"""add ansatt_cache + timer_db_cache + toggl_hours_sync task type

Phase 2 of the Toggl Track integration (the daily hours aggregator). Adds:

  - ansatt_cache: toggl_user_id → Notion `Ansatte` page id. Refreshed
    from the Ansatte DB on startup and at the top of every nightly sync.
  - timer_db_cache: year → Notion `Timer YYYY` DB id. Direct parallel to
    emails_db_cache; same year-router pattern.
  - sync_tasks.task_type widened to admit 'toggl_hours_sync'.
  - partial unique index uq_sync_tasks_active_toggl_hours enforcing
    "at most one active toggl_hours_sync ever" — global singleton so a
    cron + manual-debug double-fire collapses to one run.

Revision ID: n9k0l1m2h3i4
Revises: m8j9k0l1g2h3
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "n9k0l1m2h3i4"
down_revision: str | None = "m8j9k0l1g2h3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ansatt_cache",
        sa.Column("toggl_user_id", sa.String(length=32), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "timer_db_cache",
        sa.Column("year", sa.Integer, primary_key=True),
        sa.Column("notion_db_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Widen the CHECK constraint to admit the new literal. Postgres doesn't
    # support ALTER CHECK in place — drop + recreate.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync','oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync',"
        "'toggl_hours_sync')",
    )

    # Singleton dedup — all active toggl_hours_sync rows carry the same
    # gmail_thread_id (TOGGL_HOURS_SINGLETON_KEY in models.py), so the
    # partial unique index collapses any concurrent enqueue attempt to one.
    op.create_index(
        "uq_sync_tasks_active_toggl_hours",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'toggl_hours_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_toggl_hours", table_name="sync_tasks")

    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'toggl_hours_sync'"
    )

    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync','oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync')",
    )

    op.drop_table("timer_db_cache")
    op.drop_table("ansatt_cache")
