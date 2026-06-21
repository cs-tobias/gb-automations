"""email_content_dedup — add sent_at_minute to the PK, drop stale rows

The content-dedup layer (third defense) was missing two robustness properties
the client surfaced as a duplicate-rows-in-Notion bug: it had no protection
against trivial body drift (whitespace/case/`[image: …]` marker variance), and
two distinct short replies ("Takk!") from the same sender + project would have
collapsed into one row.

Both problems collapse into the same fix: hash a *normalized* body and add
the email's minute-precision UTC timestamp to the key. Two genuinely-distinct
replies always carry different timestamps; the same logical email re-quoted
across N threads always carries the same minute-truncated timestamp.

The table is a cache (Notion is the source of truth), so we DROP and recreate
rather than backfilling. The next sync of each thread repopulates naturally.

Revision ID: f7g8h9i0j1k2
Revises: e6f7g8h9i0j1
Create Date: 2026-06-21

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f7g8h9i0j1k2"
down_revision: str | None = "e6f7g8h9i0j1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("email_content_dedup")
    op.create_table(
        "email_content_dedup",
        sa.Column("project_page_id", sa.String(length=64), primary_key=True),
        sa.Column("from_email", sa.String(length=254), primary_key=True),
        sa.Column("sent_at_minute", sa.String(length=16), primary_key=True),
        sa.Column("body_hash", sa.String(length=64), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("email_content_dedup")
    op.create_table(
        "email_content_dedup",
        sa.Column("project_page_id", sa.String(length=64), primary_key=True),
        sa.Column("from_email", sa.String(length=254), primary_key=True),
        sa.Column("body_hash", sa.String(length=64), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column("gmail_message_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
