"""composite index project_labels (user_email, gmail_label_id)

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-05-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: str | None = "c3d4e5f6a7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # sync_thread now matches a thread to its project via a local lookup —
    # `WHERE user_email = ? AND gmail_label_id IN (...)` against project_labels —
    # instead of fetching the whole Notion project catalog per thread. This
    # composite index serves that hot path. Its leading column also covers the
    # plain `WHERE user_email = ?` webhook lookup.
    op.create_index(
        "ix_project_labels_user_label",
        "project_labels",
        ["user_email", "gmail_label_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_project_labels_user_label", table_name="project_labels")
