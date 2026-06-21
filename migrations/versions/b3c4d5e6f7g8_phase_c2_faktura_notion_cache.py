"""Phase C.2 — Faktura DB writer idempotency cache.

Per-(company_slug, record_type, fiken_record_id) marker for "we already
created a Notion page in the Faktura DB for this Fiken record." Phase
C.3's poller checks this cache before writing — a cached hit is a no-op,
so a repeated poll over the same window doesn't create duplicate Notion
rows. Make.com automation is not tracked here; the two writers coexist
during the C.1 → C.4 migration period.

Revision ID: b3c4d5e6f7g8
Revises: a2x3y4z5v6w7
Create Date: 2026-06-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7g8"
down_revision: str | None = "a2x3y4z5v6w7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "faktura_notion_cache",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("record_type", sa.String(length=16), primary_key=True),
        sa.Column("fiken_record_id", sa.String(length=32), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "record_type IN ('faktura', 'kredittnota')",
            name="faktura_notion_cache_record_type_check",
        ),
    )


def downgrade() -> None:
    op.drop_table("faktura_notion_cache")
