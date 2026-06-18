"""Fiken — add bank account cache (one bankAccountNumber per company_slug)

Engine auto-picks the first active normal-type bank account on the first
send_faktura per company, sends its `bankAccountNumber` on every draft so
the printed invoice shows a Kontonummer instead of a blank field. Cache
is bypassed when `FIKEN_BANK_ACCOUNT_NUMBER` env var is set (operator
override for multi-account setups).

Revision ID: a2x3y4z5v6w7
Revises: z1w2x3y4u5v6
Create Date: 2026-06-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a2x3y4z5v6w7"
down_revision: str | None = "z1w2x3y4u5v6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiken_bank_accounts",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("bank_account_number", sa.String(length=32), nullable=False),
        sa.Column("name_when_cached", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("fiken_bank_accounts")
