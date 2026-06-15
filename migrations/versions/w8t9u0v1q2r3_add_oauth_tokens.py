"""add oauth_tokens table

Persists rotating OAuth refresh tokens (currently just Frame.io / Adobe IMS)
so container rebuilds don't lose the rotated value. Adobe rotates the
refresh token on every refresh call and the original expires after 14
days, so we MUST persist the latest one or the integration dies. See the
OAuthToken model docstring for the full rationale.

Revision ID: w8t9u0v1q2r3
Revises: v7s8t9u0p1q2
Create Date: 2026-06-15

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "w8t9u0v1q2r3"
down_revision: str | None = "v7s8t9u0p1q2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_tokens",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("refresh_token", sa.String(length=2048), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("oauth_tokens")
