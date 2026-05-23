"""add email_content_dedup table — third dedup layer (project, from, body)

Catches duplicate Notion rows when Gmail splits the same conversation into
several distinct threads (Subject vs Svar:Subject vs Svar:Svar:Subject). The
first two dedup layers (EmailRow PK on gmail_message_id + Notion-query on
Message ID) catch same-message-different-mailbox. This new table catches
same-content-different-message-id by hashing (project, from_email, cleaned_body).

PK is the lookup key, so no separate index needed.

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-05-23

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_table("email_content_dedup")
