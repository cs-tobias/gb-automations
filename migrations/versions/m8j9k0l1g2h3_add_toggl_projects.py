"""add toggl_projects table + toggl_project_sync task type

Phase 1 of the Toggl Track integration. Adds:

  - toggl_projects: Notion page id → Toggl project id mapping. Required
    because Toggl allows duplicate-named projects in a workspace, so the
    integration MUST own the mapping (no safe name-based lookup).
  - sync_tasks.task_type widened to admit 'toggl_project_sync' literal.
  - partial unique index uq_sync_tasks_active_toggl_project enforcing
    "at most one active toggl_project_sync per project_page_id" — same
    dedup shape as uq_sync_tasks_active_frame_project / _label / _nas_folder.

No data migration needed: no rows have this task_type yet.

Revision ID: m8j9k0l1g2h3
Revises: l7i8j9k0f1g2
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "m8j9k0l1g2h3"
down_revision: str | None = "l7i8j9k0f1g2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "toggl_projects",
        sa.Column("notion_page_id", sa.String(length=64), primary_key=True),
        sa.Column("toggl_project_id", sa.String(length=32), nullable=False),
        sa.Column("toggl_workspace_id", sa.String(length=32), nullable=False),
        sa.Column("current_name", sa.String(length=255), nullable=False),
        sa.Column("toggl_url", sa.String(length=512), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    # Widen the CHECK constraint to admit the new literal. Postgres doesn't
    # support ALTER CHECK in place — drop + recreate, same trick used by the
    # Phase 2 / Phase 2.5 migrations.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync','oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync')",
    )

    # Active-dedup index — mirrors uq_sync_tasks_active_frame_project. A
    # Notion button re-fire on the same Project page collapses to one task;
    # label_sync / nas_folder_sync / frame_project_sync / toggl_project_sync
    # can all be in flight for the same project at the same time because
    # each predicate is scoped to its own task_type.
    op.create_index(
        "uq_sync_tasks_active_toggl_project",
        "sync_tasks",
        ["project_page_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'toggl_project_sync'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_toggl_project", table_name="sync_tasks")

    # Same "delete-rows-before-narrowing-CHECK" trick the Phase 2.5
    # downgrade uses, so rows with the dropped task_type don't strand the
    # constraint.
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'toggl_project_sync'"
    )

    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync','oppgave_done_sync',"
        "'leveranse_status_recheck')",
    )

    op.drop_table("toggl_projects")
