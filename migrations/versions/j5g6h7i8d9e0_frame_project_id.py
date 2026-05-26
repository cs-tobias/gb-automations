"""frame_project_folders: add frame_project_id, drop frame_parent_id + current_year

The Frame.io sync layer is being rewritten so each Notion project becomes its
own top-level Frame Project (under the configured workspace) instead of a
folder under one shared parent project. That changes what the cache row
needs to track:

  - frame_project_id (NEW): the Project entity itself, distinct from any
    folder id. What we rename via PATCH /projects/{id}, and (future work)
    toggle active/inactive on. Phase 2 comment polling will also key off this.
  - frame_folder_id (kept): now means the Project's root_folder_id (the
    parent under which discipline + task folders live). Same column name,
    different semantic — explicitly documented on the model.
  - frame_parent_id (DROPPED): used to store the year folder id. There's no
    year folder anymore, and there's no "parent" in any meaningful sense —
    the project sits directly under the workspace, which is global config.
  - current_year (DROPPED): year is no longer part of the Frame layout.

The user manually wipes both frame_*_folders tables before deploy (the
existing rows are all test data on their own Frame), so this migration runs
against an empty table in practice. Defaults on the new column are therefore
just for production correctness, not for backfill.

Revision ID: j5g6h7i8d9e0
Revises: i4f5g6h7c8d9
Create Date: 2026-05-26

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "j5g6h7i8d9e0"
down_revision: str | None = "i4f5g6h7c8d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Use a server_default of '' so the ADD COLUMN works even if any row
    # exists (test envs that forgot the manual TRUNCATE), then drop the
    # default — production correctness expects callers to set the real value.
    op.add_column(
        "frame_project_folders",
        sa.Column(
            "frame_project_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column(
        "frame_project_folders", "frame_project_id", server_default=None
    )
    op.drop_column("frame_project_folders", "frame_parent_id")
    op.drop_column("frame_project_folders", "current_year")


def downgrade() -> None:
    # Same trick in reverse: server_default lets the NOT NULL re-adds land
    # cleanly on any rows that exist, then drop the default.
    op.add_column(
        "frame_project_folders",
        sa.Column(
            "current_year",
            sa.String(length=4),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column(
        "frame_project_folders", "current_year", server_default=None
    )
    op.add_column(
        "frame_project_folders",
        sa.Column(
            "frame_parent_id",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column(
        "frame_project_folders", "frame_parent_id", server_default=None
    )
    op.drop_column("frame_project_folders", "frame_project_id")
