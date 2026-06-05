"""add project_status_dispatch task type for fast Notion-webhook ack

Adds one new sync task type so the `/webhooks/notion/project-status`
endpoint can ack Notion's HTTP request in <50ms and defer all the slow
work (Notion get_page, placeholder check, fan-out to gmail/nas/toggl/
frame + frame deliverable enumeration) to the queue worker:

  - project_status_dispatch  — webhook acks immediately; worker handler
                               reads live Notion state and enqueues all
                               the per-engine sub-tasks.

Why this matters: Notion auto-pauses webhook automations whose receiver
takes too long to respond (the timeout isn't published — community
reports + n8n issue #12257 indicate Notion's pause heuristic is
over-eager and fires on slow 200s, not just 500s). The previous shape
did 2 Notion API calls + 5 Postgres inserts inline before returning,
which on Goldbox's prod workspace was tripping the pause. The dispatch
task moves all that to the worker so the webhook only does
"validate + 1 insert + return 200".

Same shape as every previous task-type migration: widen the CHECK
constraint and add a partial unique index keyed on `project_page_id`
scoped to this task_type, so rapid Status flips on the same project
collapse to one active row.

Revision ID: u6r7s8t9o0p1
Revises: t5q6r7s8n9o0
Create Date: 2026-06-05

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "u6r7s8t9o0p1"
down_revision: str | None = "t5q6r7s8n9o0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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
        "'toggl_hours_sync','project_status_dispatch')",
    )

    op.create_index(
        "uq_sync_tasks_active_project_status_dispatch",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'project_status_dispatch'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sync_tasks_active_project_status_dispatch",
        table_name="sync_tasks",
    )
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'project_status_dispatch'"
    )
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
