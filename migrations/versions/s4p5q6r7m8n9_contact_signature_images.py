"""add contact_signature_images table

Revision ID: s4p5q6r7m8n9
Revises: r3o4p5q6l7m8
Create Date: 2026-06-01

Per-contact byte-exact image signature learning. Increments once per
(sender, sha1, thread); past `settings.signature_learn_threshold` the upload
loop skips the bytes. Catches the long-tail of MUAs that attach signature
logos as plain Content-Disposition: attachment (no cid: reference) — the
structural rule in `_partition_attachments` only catches the cid-marked case.

Keyed on sha1 (not filename), so a real screenshot named the same as a logo
is byte-different and never collides. Per-thread idempotent (`last_thread_id`)
so 8 replies in one thread carrying the same logo = +1, not +8.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "s4p5q6r7m8n9"
down_revision: str | None = "r3o4p5q6l7m8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "contact_signature_images",
        sa.Column("sender_email", sa.String(length=254), primary_key=True),
        sa.Column("content_sha1", sa.String(length=40), primary_key=True),
        sa.Column(
            "thread_seen_count",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column("last_thread_id", sa.String(length=64), nullable=False),
        sa.Column("first_filename", sa.String(length=255), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="learning",
        ),
        sa.Column(
            "first_seen_at",
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
    # Hot-path lookup is `WHERE sender_email = ? AND content_sha1 = ?` —
    # already covered by the composite PK with sender_email leading. No
    # secondary index needed.


def downgrade() -> None:
    op.drop_table("contact_signature_images")
