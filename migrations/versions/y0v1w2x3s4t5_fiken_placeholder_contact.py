"""Fiken — placeholder contact cache for missing-Orgnr drafts

A "Mangler kunde" Fiken contact stands in when the project's
Fakturamottaker has no Orgnr yet. Previously the engine created a
draft with NO customerId in that case — Fiken's docs say customerId
is required and rejects the POST in practice. Linking a placeholder
contact instead keeps the draft creatable; the operator picks/edits
the real customer in Fiken's UI before clicking Send.

One placeholder per Fiken company_slug (single-tenant Goldbox today,
multi-tenant later). The cache stores the contactId so we reuse the
same placeholder instead of minting a new orphan contact every run.

Revision ID: y0v1w2x3s4t5
Revises: x9u0v1w2r3s4
Create Date: 2026-06-18

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "y0v1w2x3s4t5"
down_revision: str | None = "x9u0v1w2r3s4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiken_placeholder_contacts",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("fiken_contact_id", sa.String(length=32), nullable=False),
        sa.Column("name_when_created", sa.String(length=128), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("fiken_placeholder_contacts")
