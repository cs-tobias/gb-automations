"""phase 2.5 status loop: widen sync_tasks CHECK + add 3 partial unique indexes

Adds three new task types for the Leveranser status loop:

  - frame_version_sync       — file.versioned webhook → flip status to "Ferdig"
  - oppgave_done_sync        — Notion checkbox toggled → PATCH Frame comment + recheck
  - leveranse_status_recheck — recompute rollup → Under arbeid / Oppgaver ferdig

Each gets a partial unique index on `gmail_thread_id` scoped to its
task_type so rapid-fire webhook deliveries collapse to one active row.
No new table — the status itself lives entirely in Notion's Status
select on the Leveranser DB; we don't cache it.

Revision ID: l7i8j9k0f1g2
Revises: k6h7i8j9e0f1
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "l7i8j9k0f1g2"
down_revision: str | None = "k6h7i8j9e0f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Widen the CHECK constraint to allow the three new literals. Following
    # the Phase 2 pattern: drop + recreate (Postgres doesn't support ALTER
    # CHECK in place). No row UPDATE needed — no rows have these task
    # types yet.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync','oppgave_done_sync',"
        "'leveranse_status_recheck')",
    )

    # Three partial unique indexes — same shape as Phase 2's frame_comment
    # index. The dedup key (gmail_thread_id) carries different things
    # depending on task_type, but the column slot is shared because we
    # don't want to add task-type-specific columns to sync_tasks.
    op.create_index(
        "uq_sync_tasks_active_frame_version",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_version_sync'"
        ),
    )
    op.create_index(
        "uq_sync_tasks_active_oppgave_done",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'oppgave_done_sync'"
        ),
    )
    op.create_index(
        "uq_sync_tasks_active_leveranse_status",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'leveranse_status_recheck'"
        ),
    )


def downgrade() -> None:
    # Inverse of upgrade. Same "widen-CHECK-first-then-narrow" trick as
    # the Phase 2 downgrade so rows using the new task types don't get
    # stranded by a constraint violation: first delete any such rows,
    # then narrow the CHECK back.
    op.drop_index(
        "uq_sync_tasks_active_leveranse_status", table_name="sync_tasks"
    )
    op.drop_index(
        "uq_sync_tasks_active_oppgave_done", table_name="sync_tasks"
    )
    op.drop_index(
        "uq_sync_tasks_active_frame_version", table_name="sync_tasks"
    )

    op.execute(
        "DELETE FROM sync_tasks WHERE task_type IN "
        "('frame_version_sync','oppgave_done_sync','leveranse_status_recheck')"
    )

    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync')",
    )
