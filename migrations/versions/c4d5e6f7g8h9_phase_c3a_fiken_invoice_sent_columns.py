"""Phase C.3a — add sent-invoice graduation columns to fiken_invoices.

Adds four nullable columns:
  - draft_uuid     — Fiken draft's `uuid` (set at send_faktura time).
                     The poller / manual trigger matches a sent invoice
                     back to this row via Fiken's `invoiceDraftUuid`
                     (Fiken mints a new `invoiceId` at send-time, so
                     the draft uuid is the only stable FK).
  - sent_at        — graduation marker timestamp.
  - sent_url       — the Fiken UI URL we wrote into the Faktura DB
                     row's URL column.
  - invoice_number — Fiken's printable invoiceNumber ("10042").

All four are NULL on existing rows; backfill happens organically as
new send_faktura runs (which populate draft_uuid at draft creation)
and graduations (which populate the three sent_* columns) take place.

Revision ID: c4d5e6f7g8h9
Revises: b3c4d5e6f7g8
Create Date: 2026-06-20

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4d5e6f7g8h9"
down_revision: str | None = "b3c4d5e6f7g8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "fiken_invoices",
        sa.Column("draft_uuid", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_fiken_invoices_draft_uuid",
        "fiken_invoices",
        ["draft_uuid"],
    )
    op.add_column(
        "fiken_invoices",
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "fiken_invoices",
        sa.Column("sent_url", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "fiken_invoices",
        sa.Column(
            "invoice_number", sa.String(length=32), nullable=True
        ),
    )


def downgrade() -> None:
    op.drop_column("fiken_invoices", "invoice_number")
    op.drop_column("fiken_invoices", "sent_url")
    op.drop_column("fiken_invoices", "sent_at")
    op.drop_index("ix_fiken_invoices_draft_uuid", "fiken_invoices")
    op.drop_column("fiken_invoices", "draft_uuid")
