"""Add `graduate_faktura` task type + dedup index.

Adds one new sync task type so the `/webhooks/notion/graduate-faktura`
button (operator's manual "check Fiken for sent invoices for this
project" fallback) can enqueue work that the queue worker dispatches
to `graduate_project`.

  - Widen `ck_sync_tasks_task_type` to admit `graduate_faktura`.
  - Add `uq_sync_tasks_active_graduate_faktura` partial-unique index
    keyed on `gmail_thread_id` (which we use to hold the project page
    id), scoped to active rows of this task type. Mirrors
    `uq_sync_tasks_active_send_faktura` (from migration x9u0v1w2r3s4).

Revision ID: d5e6f7g8h9i0
Revises: c4d5e6f7g8h9
Create Date: 2026-06-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5e6f7g8h9i0"
down_revision: str | None = "c4d5e6f7g8h9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Current live whitelist before this migration (see live Postgres
# `pg_get_constraintdef`). New entry appended at the end.
_TASK_TYPES_WITH_GRADUATE = (
    "'thread','label_sync','nas_folder_sync','task_folder_sync',"
    "'frame_project_sync','frame_leveranse_sync','frame_comment_sync',"
    "'frame_version_sync','frame_file_status_sync',"
    "'frame_project_status_sync','oppgave_done_sync',"
    "'leveranse_status_recheck','toggl_project_sync','toggl_hours_sync',"
    "'project_status_dispatch','fiken_invoice_create','send_faktura',"
    "'fiken_poll','graduate_faktura'"
)
_TASK_TYPES_WITHOUT_GRADUATE = (
    "'thread','label_sync','nas_folder_sync','task_folder_sync',"
    "'frame_project_sync','frame_leveranse_sync','frame_comment_sync',"
    "'frame_version_sync','frame_file_status_sync',"
    "'frame_project_status_sync','oppgave_done_sync',"
    "'leveranse_status_recheck','toggl_project_sync','toggl_hours_sync',"
    "'project_status_dispatch','fiken_invoice_create','send_faktura',"
    "'fiken_poll'"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_sync_tasks_task_type", "sync_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        f"task_type IN ({_TASK_TYPES_WITH_GRADUATE})",
    )

    op.create_index(
        "uq_sync_tasks_active_graduate_faktura",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') "
            "AND task_type = 'graduate_faktura'"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_sync_tasks_active_graduate_faktura", table_name="sync_tasks"
    )
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'graduate_faktura'"
    )
    op.drop_constraint(
        "ck_sync_tasks_task_type", "sync_tasks", type_="check"
    )
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        f"task_type IN ({_TASK_TYPES_WITHOUT_GRADUATE})",
    )
