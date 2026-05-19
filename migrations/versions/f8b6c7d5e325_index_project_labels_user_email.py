"""index project_labels.user_email

Revision ID: f8b6c7d5e325
Revises: e7a5b6c4d214
Create Date: 2026-05-19

"""

from collections.abc import Sequence

from alembic import op

revision: str = "f8b6c7d5e325"
down_revision: str | None = "e7a5b6c4d214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The composite PK is (notion_page_id, user_email) in that column order, so
    # single-column lookups by user_email can't use the PK index. The Gmail
    # webhook hot path queries `WHERE user_email = ?` on every push to decide
    # whether the push touches a project label — a real index keeps that lookup
    # microsecond-scale even as project_labels grows.
    op.create_index(
        "ix_project_labels_user_email",
        "project_labels",
        ["user_email"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_labels_user_email", table_name="project_labels")
