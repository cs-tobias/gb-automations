"""add attachment_fingerprints table

Revision ID: c5e3d4f2a092
Revises: b4d2c1f9e081
Create Date: 2026-05-14

Tracks (sender_email, content_sha1) repetitions so we can detect signature
images: an image hash seen 2+ times from the same sender is almost always a
signature decoration and skipped from Drive upload. First sighting still
uploads; second sighting and beyond skip.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c5e3d4f2a092"
down_revision: str | None = "b4d2c1f9e081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("attachment_fingerprints")
