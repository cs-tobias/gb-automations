"""add drive_links to thread_attachments

Revision ID: c7f1a2b3d495
Revises: b5e3d2c0f147
Create Date: 2026-05-20

The thread_attachments table deduped Drive uploads by (thread, content_sha1) but
stored only the fact that the bytes were uploaded — not where. A re-sync skips
the upload (sha1 already known) and so produced an empty `uploaded` list, which
meant the Notion row's Files property was never (re)set: "already on Drive"
silently doubled as "can't re-link", leaving rows file-less while the bytes sat
in Drive. `drive_links` records the {name, url} entries (one per matched project
subfolder) so a skip can still re-link the row. Nullable so existing rows are
treated as "no known links" (they re-link on the next genuine upload).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c7f1a2b3d495"
down_revision: str | None = "b5e3d2c0f147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "thread_attachments",
        sa.Column("drive_links", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("thread_attachments", "drive_links")
