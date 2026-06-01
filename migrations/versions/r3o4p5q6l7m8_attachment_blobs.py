"""replace thread_attachments + attachment_fingerprints with attachment_blobs

Revision ID: r3o4p5q6l7m8
Revises: q2n3o4p5k6l7
Create Date: 2026-06-01

Cross-thread attachment dedup. The old `thread_attachments` table keyed on
`(gmail_thread_id, content_sha1)` so the same bytes uploaded fresh in every
thread they appeared in, even when sharing the same Drive folder. The new
`attachment_blobs` table keys on `(content_sha1, drive_folder_path)` instead:
identical bytes landing in the same project folder reuse the existing Drive
link no matter which thread carried them.

`attachment_fingerprints` is dropped at the same time: it was added for a
statistical sender-repeat signature-detection heuristic that was never wired
up (the table has always been empty in production) and has been replaced by
a structural MIME-based rule in `_partition_attachments` that doesn't need a
per-sender count.

NO data backfill. Existing Drive URLs stay live on Notion rows (they're
stored on the row itself, not in these tables). Re-running the per-project
"Sync to Gmail" button in Notion (which archives the thread's rows and
re-syncs from scratch) repopulates `attachment_blobs` with the correct
folder paths as it goes, naturally accruing the cross-thread dedup benefit.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r3o4p5q6l7m8"
down_revision: str | None = "q2n3o4p5k6l7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attachment_blobs",
        sa.Column("content_sha1", sa.String(length=40), primary_key=True),
        sa.Column("drive_folder_path", sa.String(length=1024), primary_key=True),
        sa.Column("drive_name", sa.String(length=255), nullable=False),
        sa.Column("drive_url", sa.String(length=2048), nullable=False),
        sa.Column("first_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.drop_table("thread_attachments")
    op.drop_table("attachment_fingerprints")


def downgrade() -> None:
    # Recreate the dropped tables empty; their data is not preserved on the
    # way down — the new pipeline is one-way. The old `drive_links` column on
    # thread_attachments is restored as nullable JSONB to match the schema at
    # head of c7f1a2b3d495.
    op.create_table(
        "attachment_fingerprints",
        sa.Column("sender_email", sa.String(length=254), primary_key=True),
        sa.Column("content_sha1", sa.String(length=40), primary_key=True),
        sa.Column("seen_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
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
    op.create_table(
        "thread_attachments",
        sa.Column("gmail_thread_id", sa.String(length=64), primary_key=True),
        sa.Column("content_sha1", sa.String(length=40), primary_key=True),
        sa.Column("first_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "drive_links",
            sa.dialects.postgresql.JSONB(),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.drop_table("attachment_blobs")
