"""fiken_invoice_lines.discipline → nullable

Eligibility no longer rejects rows whose Type isn't a recognized discipline
(free-text / internal-task rows are billable now and land on the draft as a
free-text line). For those rows `InvoiceLine.discipline` is None, so the audit
INSERT into fiken_invoice_lines hit a NOT NULL violation and crashed
send_faktura. Relax the column to nullable; existing rows are unaffected.

Revision ID: g8h9i0j1k2l3
Revises: f7g8h9i0j1k2
Create Date: 2026-06-22

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "g8h9i0j1k2l3"
down_revision: str | None = "f7g8h9i0j1k2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "fiken_invoice_lines",
        "discipline",
        existing_type=sa.String(length=32),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "fiken_invoice_lines",
        "discipline",
        existing_type=sa.String(length=32),
        nullable=False,
    )
