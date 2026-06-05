"""add frame_project_status_sync task type for project active/inactive mirror

Adds one new sync task type used by the Notion-only project lifecycle
mirror to Frame.io's V4 active/inactive flag:

  - frame_project_status_sync  — Notion Projects-DB `Status` changed
                                 → PATCH the project's Frame.io entity
                                 status to "inactive" (Ferdig/Tapt) or
                                 "active" (anything else).

Same shape as the Phase 2.5 status loop / Utgår reconcile migrations:
widen the CHECK constraint to allow the new task_type, and add a
partial unique index keyed on `project_page_id` scoped to this
task_type, so rapid Status flips on the same project collapse to one
active row (the engine reads live Notion at process time).

Revision ID: t5q6r7s8n9o0
Revises: s4p5q6r7m8n9
Create Date: 2026-06-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "t5q6r7s8n9o0"
down_revision: str | None = "s4p5q6r7m8n9"
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
        "'frame_file_status_sync','frame_project_status_sync',"
        "'oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync',"
        "'toggl_hours_sync')",
    )

    op.create_index(
        "uq_sync_tasks_active_frame_project_status",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_project_status_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sync_tasks_active_frame_project_status",
        table_name="sync_tasks",
    )
    # Strand any rows of the new type before narrowing the CHECK (same
    # belt-and-suspenders trick as the Phase 2.5 / Utgår downgrades).
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'frame_project_status_sync'"
    )
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
