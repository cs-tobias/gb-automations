"""add frame_korreksjonsrunder dedup cache

The lazily-created "Korreksjonsrunde N" sub-row was deduped via a Notion
database query (find_korreksjonsrunde_row). Notion's query index is
eventually consistent and lags page creation by seconds, so when two
comments of a fresh round arrive in a burst the second comment's query
doesn't see the round row the first one just created → it creates a
duplicate "Korreksjonsrunde N" row. This table is a read-your-writes
Postgres cache keyed on (leveranse_page_id, round_number) that the
engine consults before falling back to the Notion query, closing the
race. Notion remains the source of truth (same contract as the other
*_cache tables).

Revision ID: p1m2n3o4j5k6
Revises: o0l1m2n3i4j5
Create Date: 2026-05-29

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p1m2n3o4j5k6"
down_revision: str | None = "o0l1m2n3i4j5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "frame_korreksjonsrunder",
        sa.Column("leveranse_page_id", sa.String(length=64), primary_key=True),
        sa.Column("round_number", sa.Integer(), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("frame_korreksjonsrunder")
