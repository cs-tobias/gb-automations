"""add frame_file_status_sync task type for Utgår reconcile

Adds one new sync task type used by the bidirectional Notion ↔ Frame.io
Utgår status mirror:

  - frame_file_status_sync  — Notion deliverable Status changed OR Frame
                              custom Status field changed → reconcile.

Same shape as the Phase 2.5 status loop migration: widen the CHECK
constraint and add a partial unique index keyed on `gmail_thread_id`
scoped to this task_type, so a Notion-side + Frame-side nudge for the
same deliverable collapses to one active row.

Revision ID: q2n3o4p5k6l7
Revises: p1m2n3o4j5k6
Create Date: 2026-06-01

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "q2n3o4p5k6l7"
down_revision: str | None = "p1m2n3o4j5k6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widen the CHECK constraint. Postgres doesn't support ALTER CHECK
    # in place — drop + recreate. No row UPDATE needed; no rows carry
    # the new task type yet.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync',"
        "'frame_file_status_sync','oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync',"
        "'toggl_hours_sync')",
    )

    op.create_index(
        "uq_sync_tasks_active_frame_file_status",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_file_status_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sync_tasks_active_frame_file_status", table_name="sync_tasks"
    )
    # Strand any rows of the new type before narrowing the CHECK (same
    # belt-and-suspenders trick as the Phase 2.5 downgrade).
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'frame_file_status_sync'"
    )
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
