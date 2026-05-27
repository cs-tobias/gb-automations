"""phase 2 frame comments: rename frame_task_folders, add frame_comments,
widen sync_tasks task_type CHECK, rename + add active-dedup indexes

Phase 2 reframes what a row in the Frame folder cache represents: it's now
a *Leveranse* (deliverable entity / image), not a task. The table is
renamed and the corresponding sync task type follows
(frame_task_sync → frame_leveranse_sync). A new frame_comment_sync task
type lands at the same time, keyed off the new frame_comments cache
table.

This migration:
  1. RENAME frame_task_folders → frame_leveranse_folders. Column names
     unchanged; the semantic shift is "what does a row mean", not "what
     does each column hold".
  2. UPDATE any existing sync_tasks rows from task_type='frame_task_sync'
     → 'frame_leveranse_sync'. Test-data only in practice (the manual
     truncate workflow before deploy means production has zero rows of
     this type); the UPDATE is here for correctness in dev envs.
  3. DROP + RECREATE ck_sync_tasks_task_type so the CHECK allows
     'frame_leveranse_sync' and 'frame_comment_sync', drops
     'frame_task_sync'.
  4. DROP uq_sync_tasks_active_frame_task, CREATE
     uq_sync_tasks_active_frame_leveranse with the new task_type literal
     in its WHERE clause. Partial-index predicates can't be ALTERed in
     Postgres — must drop + recreate.
  5. CREATE TABLE frame_comments (the Phase 2 cache: one row per Frame
     comment we've synced, with the Notion bullet block id we wrote it
     to so replies can find their parent block for indent).
  6. CREATE uq_sync_tasks_active_frame_comment partial unique index for
     enqueue dedup on webhook redelivery.

Downgrade reverses each step in inverse order. The frame_comments table
is dropped (data loss on downgrade is acceptable — re-running the
webhook will repopulate). The task_type rename is reversed both in the
CHECK and in any existing rows.

Revision ID: k6h7i8j9e0f1
Revises: j5g6h7i8d9e0
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "k6h7i8j9e0f1"
down_revision: str | None = "j5g6h7i8d9e0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Rename the table. PK + indexes follow the table automatically in
    # Postgres; only named partial indexes care about the table name.
    op.rename_table("frame_task_folders", "frame_leveranse_folders")

    # 2. Widen the CHECK constraint FIRST, so the row UPDATE in step 3
    # doesn't violate the old, narrower constraint. Add the new
    # 'frame_leveranse_sync' and 'frame_comment_sync' literals while
    # *keeping* the old 'frame_task_sync' temporarily so existing rows
    # remain valid until step 3 moves them across.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_task_sync',"
        "'frame_leveranse_sync','frame_comment_sync')",
    )

    # 3. Move any existing test-env rows to the new task_type literal.
    # Test-data only in practice; the wider CHECK from step 2 makes this
    # UPDATE safe even when rows exist.
    op.execute(
        "UPDATE sync_tasks "
        "SET task_type = 'frame_leveranse_sync' "
        "WHERE task_type = 'frame_task_sync'"
    )

    # 4. Now that no rows reference the legacy literal anymore, narrow
    # the CHECK back down to drop 'frame_task_sync'.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync')",
    )

    # 5. Replace the frame_task active-dedup index with one that gates
    # on the new task_type literal. Partial-index WHERE clauses are part
    # of the index definition, so drop + create is required.
    op.drop_index("uq_sync_tasks_active_frame_task", table_name="sync_tasks")
    op.create_index(
        "uq_sync_tasks_active_frame_leveranse",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_leveranse_sync'"
        ),
    )

    # 6. The frame_comments cache. PK is the Frame comment UUID so
    # `INSERT ... ON CONFLICT (frame_comment_id) DO NOTHING` gives engine-
    # level idempotency. notion_block_id is nullable because we write the
    # row AFTER the Notion append succeeds (Decision 3 in the plan).
    op.create_table(
        "frame_comments",
        sa.Column("frame_comment_id", sa.String(length=64), primary_key=True),
        sa.Column("frame_file_id", sa.String(length=64), nullable=False),
        sa.Column("leveranse_page_id", sa.String(length=64), nullable=False),
        sa.Column("oppgave_page_id", sa.String(length=64), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("parent_comment_id", sa.String(length=64), nullable=True),
        sa.Column("notion_block_id", sa.String(length=64), nullable=True),
        sa.Column(
            "body_snippet",
            sa.String(length=512),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_frame_comments_file_round",
        "frame_comments",
        ["frame_file_id", "round_number"],
    )
    op.create_index(
        "ix_frame_comments_oppgave",
        "frame_comments",
        ["oppgave_page_id"],
    )

    # 7. Active-dedup index for frame_comment_sync queue rows.
    op.create_index(
        "uq_sync_tasks_active_frame_comment",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_comment_sync'"
        ),
    )


def downgrade() -> None:
    # Inverse of upgrade, in reverse step order.

    op.drop_index("uq_sync_tasks_active_frame_comment", table_name="sync_tasks")

    op.drop_index("ix_frame_comments_oppgave", table_name="frame_comments")
    op.drop_index("ix_frame_comments_file_round", table_name="frame_comments")
    op.drop_table("frame_comments")

    op.drop_index(
        "uq_sync_tasks_active_frame_leveranse", table_name="sync_tasks"
    )
    op.create_index(
        "uq_sync_tasks_active_frame_task",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'frame_task_sync'"
        ),
    )

    # Widen the CHECK to include both old AND new literals so the row
    # UPDATE/DELETE below doesn't violate it; then narrow once the data
    # is back on the legacy literal. Symmetric to the upgrade.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_task_sync',"
        "'frame_leveranse_sync','frame_comment_sync')",
    )

    # Revert any rows we updated. Comment-sync rows can't be downgraded
    # (no equivalent in the prior schema) — delete them; they have no
    # folders/files to point to anyway.
    op.execute(
        "DELETE FROM sync_tasks WHERE task_type = 'frame_comment_sync'"
    )
    op.execute(
        "UPDATE sync_tasks "
        "SET task_type = 'frame_task_sync' "
        "WHERE task_type = 'frame_leveranse_sync'"
    )

    # Now narrow the CHECK back to the pre-Phase-2 set.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_task_sync')",
    )

    op.rename_table("frame_leveranse_folders", "frame_task_folders")
