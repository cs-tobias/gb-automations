"""add Fiken Phase A — tables, task types, partial-unique indices

Phase A of the Fiken integration (see docs/misc/fiken-integration.md). The
schema lands first, gated by the SYNC_FIKEN feature flag (default OFF), so
the engines + webhooks in Phase B/C can build on top without a follow-up
migration. Three things happen here:

  1. Five new tables:
       - fiken_invoices: one row per Fiken invoice we've touched (creation
         audit trail + poller dedup cache; see model docstring).
       - fiken_invoice_lines: one row per Oppgave billed on a given
         invoice — the per-row audit trail behind the remainder math.
       - fiken_product_cache: per-discipline Fiken product mirror.
       - fakturaer_db_cache: year → Notion `Fakturaer YYYY` DB id.
       - tilbud_db_cache: year → Notion `Tilbud YYYY` DB id.

  2. Widen ck_sync_tasks_task_type to admit `fiken_invoice_create` and
     `fiken_poll`.

  3. Two new partial-unique indices on sync_tasks:
       - uq_sync_tasks_active_fiken_invoice_create — keyed on
         gmail_thread_id, which the enqueue helper packs as
         f"{project_page_id}:{invoice_type}" so oppstart + slutt for the
         same project don't collide but a double-click on the same button
         does.
       - uq_sync_tasks_active_fiken_poll — global singleton, mirrors
         uq_sync_tasks_active_toggl_hours; the enqueue helper stuffs
         FIKEN_POLL_SINGLETON_KEY into gmail_thread_id.

Revision ID: v7s8t9u0p1q2
Revises: u6r7s8t9o0p1
Create Date: 2026-06-12

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "v7s8t9u0p1q2"
down_revision: str | None = "u6r7s8t9o0p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "fiken_invoices",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("fiken_invoice_id", sa.String(length=32), primary_key=True),
        sa.Column("project_page_id", sa.String(length=64), nullable=True),
        sa.Column("notion_page_id", sa.String(length=64), nullable=True),
        sa.Column("invoice_type", sa.String(length=16), nullable=True),
        sa.Column("invoice_fraction", sa.Float, nullable=True),
        sa.Column("last_modified_date", sa.String(length=32), nullable=True),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_fiken_invoices_project_page_id",
        "fiken_invoices",
        ["project_page_id"],
    )

    op.create_table(
        "fiken_invoice_lines",
        sa.Column("company_slug", sa.String(length=64), primary_key=True),
        sa.Column("fiken_invoice_id", sa.String(length=32), primary_key=True),
        sa.Column("oppgave_page_id", sa.String(length=64), primary_key=True),
        sa.Column("discipline", sa.String(length=32), nullable=False),
        sa.Column("fraction", sa.Float, nullable=False),
        sa.Column("unit_price_ore", sa.Integer, nullable=False),
        sa.Column(
            "inserted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_fiken_invoice_lines_oppgave",
        "fiken_invoice_lines",
        ["oppgave_page_id"],
    )

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

    op.create_table(
        "fakturaer_db_cache",
        sa.Column("year", sa.Integer, primary_key=True),
        sa.Column("notion_db_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    op.create_table(
        "tilbud_db_cache",
        sa.Column("year", sa.Integer, primary_key=True),
        sa.Column("notion_db_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )

    # Widen the CHECK constraint to admit fiken_invoice_create + fiken_poll.
    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync',"
        "'frame_file_status_sync','frame_project_status_sync',"
        "'oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync',"
        "'toggl_hours_sync','project_status_dispatch',"
        "'fiken_invoice_create','fiken_poll')",
    )

    op.create_index(
        "uq_sync_tasks_active_fiken_invoice_create",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'fiken_invoice_create'"
        ),
    )
    op.create_index(
        "uq_sync_tasks_active_fiken_poll",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'fiken_poll'"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_tasks_active_fiken_poll", table_name="sync_tasks")
    op.drop_index(
        "uq_sync_tasks_active_fiken_invoice_create", table_name="sync_tasks"
    )

    op.execute(
        "DELETE FROM sync_tasks WHERE task_type IN ('fiken_invoice_create','fiken_poll')"
    )

    op.drop_constraint("ck_sync_tasks_task_type", "sync_tasks", type_="check")
    op.create_check_constraint(
        "ck_sync_tasks_task_type",
        "sync_tasks",
        "task_type IN ('thread','label_sync','nas_folder_sync',"
        "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
        "'frame_comment_sync','frame_version_sync',"
        "'frame_file_status_sync','frame_project_status_sync',"
        "'oppgave_done_sync',"
        "'leveranse_status_recheck','toggl_project_sync',"
        "'toggl_hours_sync','project_status_dispatch')",
    )

    op.drop_table("tilbud_db_cache")
    op.drop_table("fakturaer_db_cache")
    op.drop_table("fiken_product_cache")
    op.drop_index(
        "ix_fiken_invoice_lines_oppgave", table_name="fiken_invoice_lines"
    )
    op.drop_table("fiken_invoice_lines")
    op.drop_index(
        "ix_fiken_invoices_project_page_id", table_name="fiken_invoices"
    )
    op.drop_table("fiken_invoices")
