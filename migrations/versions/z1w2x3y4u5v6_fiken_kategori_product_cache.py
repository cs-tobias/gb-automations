"""Fiken — replace discipline product catalogue with kategori → productId cache

We're switching from auto-maintaining a discipline-based product catalogue
(`fiken_product_cache`, keyed on canonical discipline) to operator-managed
Fiken products that we resolve by name match against Notion's `Oppgave
kategori` multi-select label.

Cleanup + replacement:
  - DROP `fiken_product_cache`. The engine no longer auto-creates Fiken
    products; that catalogue was never actually linked to invoice lines
    (we sent free-text + `incomeAccount` instead of `productId`).
  - ADD `fiken_product_by_kategori`. Single lookup table keyed on
    (company_slug, kategori_label) → fiken_product_id. Populated lazily
    on the first send_faktura that references each kategori.

Downgrade re-creates `fiken_product_cache` for symmetry but doesn't
backfill — the data was per-discipline and isn't portable to the new
per-kategori shape. Operators rerunning the discipline-based engine
would re-populate it from Fiken's API on the first run.

Revision ID: z1w2x3y4u5v6
Revises: y0v1w2x3s4t5
Create Date: 2026-06-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z1w2x3y4u5v6"
down_revision: str | None = "y0v1w2x3s4t5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("fiken_product_cache")

    op.create_table(
        "fiken_product_by_kategori",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("kategori_label", sa.String(length=128), primary_key=True),
        sa.Column("fiken_product_id", sa.String(length=32), nullable=False),
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
    op.drop_table("fiken_product_by_kategori")

    op.create_table(
        "fiken_product_cache",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("discipline", sa.String(length=32), primary_key=True),
        sa.Column("fiken_product_id", sa.String(length=32), nullable=False),
        sa.Column("product_number", sa.String(length=64), nullable=False),
        sa.Column("last_unit_price_ore", sa.Integer, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
