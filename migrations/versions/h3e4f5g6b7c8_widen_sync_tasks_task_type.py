"""widen sync_tasks.task_type from VARCHAR(16) to VARCHAR(32)

The original column was sized for 'task_folder_sync' (16 chars exactly). The
Phase 1 Frame.io integration adds 'frame_project_sync' (18 chars), which
overflows and aborts the webhook handler before any task type can be enqueued
(StringDataRightTruncationError, surfaced to Notion as "failed to execute").

Widen once now to 32 so future task type names have headroom too — no schema
churn next time we add a sync target. ALTER on a VARCHAR width change is a
Postgres metadata-only update; no row rewrite, no lock escalation worth
mentioning at our table size.

The downgrade narrows back to VARCHAR(16) and WILL fail if any frame_project_sync
rows exist at downgrade time — that's correct: Postgres refusing to silently
truncate data is the desired behavior on a rollback that would lose information.

Revision ID: h3e4f5g6b7c8
Revises: g2c3d4e5f6a7
Create Date: 2026-05-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "h3e4f5g6b7c8"
down_revision: str | None = "g2c3d4e5f6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "sync_tasks",
        "task_type",
        type_=sa.String(length=32),
        existing_type=sa.String(length=16),
        existing_nullable=False,
        existing_server_default="thread",
    )


def downgrade() -> None:
    op.alter_column(
        "sync_tasks",
        "task_type",
        type_=sa.String(length=16),
        existing_type=sa.String(length=32),
        existing_nullable=False,
        existing_server_default="thread",
    )
