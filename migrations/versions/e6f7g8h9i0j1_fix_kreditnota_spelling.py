"""Fix spelling: 'kredittnota' → 'kreditnota' in record_type values.

The Faktura DB schema and our code originally used 'Kredittnota' (two
t's) for both the Notion-facing Type select option AND the internal
`record_type` identifier on `faktura_notion_cache`. Norwegian spelling
is `Kreditnota` (one t). Code + Notion options are being renamed in
lockstep; this migration brings Postgres along:

  1. Drop the CHECK constraint that whitelisted 'kredittnota'.
  2. UPDATE existing cache rows from 'kredittnota' → 'kreditnota'.
  3. Re-create the CHECK constraint allowing 'kreditnota'.

Idempotency: the UPDATE is a no-op when nothing matches the old value,
and the CHECK rebuild always installs the new spelling regardless of
prior state. Safe to run repeatedly via downgrade + upgrade.

Revision ID: e6f7g8h9i0j1
Revises: d5e6f7g8h9i0
Create Date: 2026-06-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "e6f7g8h9i0j1"
down_revision: str | None = "d5e6f7g8h9i0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # CHECK constraint must be dropped before we can flip the data —
    # otherwise the UPDATE would violate it transiently.
    op.drop_constraint(
        "faktura_notion_cache_record_type_check",
        "faktura_notion_cache",
        type_="check",
    )
    op.execute(
        "UPDATE faktura_notion_cache "
        "SET record_type = 'kreditnota' "
        "WHERE record_type = 'kredittnota'"
    )
    op.create_check_constraint(
        "faktura_notion_cache_record_type_check",
        "faktura_notion_cache",
        "record_type IN ('faktura', 'kreditnota')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "faktura_notion_cache_record_type_check",
        "faktura_notion_cache",
        type_="check",
    )
    op.execute(
        "UPDATE faktura_notion_cache "
        "SET record_type = 'kredittnota' "
        "WHERE record_type = 'kreditnota'"
    )
    op.create_check_constraint(
        "faktura_notion_cache_record_type_check",
        "faktura_notion_cache",
        "record_type IN ('faktura', 'kredittnota')",
    )
