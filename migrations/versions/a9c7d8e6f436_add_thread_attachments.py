"""add thread_attachments table

Revision ID: a9c7d8e6f436
Revises: f8b6c7d5e325
Create Date: 2026-05-20

Durable, thread-scoped record of attachment content hashes already uploaded to
Drive. The in-memory ThreadAttachmentTracker only dedups within one sync; a new
reply re-carries the quoted MIME tree, so without this table every reply
re-uploads the same bytes. Keyed by (gmail_thread_id, content_sha1) so an exact
byte match anywhere in the thread means "already on Drive, skip".
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a9c7d8e6f436"
down_revision: str | None = "f8b6c7d5e325"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thread_attachments",
        sa.Column("gmail_thread_id", sa.String(length=64), primary_key=True),
        sa.Column("content_sha1", sa.String(length=40), primary_key=True),
        sa.Column("first_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("thread_attachments")
