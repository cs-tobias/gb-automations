"""add emails_db_cache table

Revision ID: d6f4e5g3b103
Revises: c5e3d4f2a092
Create Date: 2026-05-15

Year → Notion DB ID cache for the year-partitioned Emails databases. The
year router auto-creates `Emails YYYY` DBs under EMAILS_PARENT_PAGE_ID on
first email of each year; this table persists the resolved IDs so restarts
don't trigger a Notion search per request, and the webhook feedback-loop
filter recognizes "our own writes" across every year DB.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d6f4e5g3b103"
down_revision: str | None = "c5e3d4f2a092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "emails_db_cache",
        sa.Column("year", sa.Integer(), primary_key=True),
        sa.Column("notion_db_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("emails_db_cache")
