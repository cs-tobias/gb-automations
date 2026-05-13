"""add email_rows and contact_cache tables

Revision ID: a3185a1531dc
Revises: 2f2aaed71dde
Create Date: 2026-05-13

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3185a1531dc"
down_revision: str | None = "2f2aaed71dde"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "email_rows",
        sa.Column("gmail_message_id", sa.String(length=64), primary_key=True),
        sa.Column("gmail_thread_id", sa.String(length=64), nullable=False),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column("seen_by_email", sa.String(length=254), nullable=True),
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
    op.create_index("ix_email_rows_gmail_thread_id", "email_rows", ["gmail_thread_id"])

    op.create_table(
        "contact_cache",
        sa.Column("email", sa.String(length=254), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("contact_cache")
    op.drop_index("ix_email_rows_gmail_thread_id", table_name="email_rows")
    op.drop_table("email_rows")
