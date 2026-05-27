"""drop ansatt_cache; add toggl_user_cache

Refactor of the Toggl hours user-mapping layer. Goldbox dropped the
hand-curated `Ansatte` Notion DB in favor of native Notion user accounts:
the engine now reads Notion's workspace user list and matches each Toggl
user to a Notion user by email. The local cache only needs to hold the
Toggl side of that map (user_id → email + name); the Notion side is
fetched per-sync and held in process.

  - `ansatt_cache` becomes obsolete and is dropped.
  - `toggl_user_cache` replaces it with the Toggl-side roster.

Idempotent: no rows depend on the old table outside this slice.

Revision ID: o0l1m2n3i4j5
Revises: n9k0l1m2h3i4
Create Date: 2026-05-27

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "o0l1m2n3i4j5"
down_revision: str | None = "n9k0l1m2h3i4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "toggl_user_cache",
        sa.Column("toggl_user_id", sa.String(length=32), primary_key=True),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.drop_table("ansatt_cache")


def downgrade() -> None:
    op.create_table(
        "ansatt_cache",
        sa.Column("toggl_user_id", sa.String(length=32), primary_key=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=False),
        sa.Column(
            "name",
            sa.String(length=255),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )

    op.drop_table("toggl_user_cache")
