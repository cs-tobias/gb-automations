"""Fiken integration v2 — single 'Send faktura' button, label-driven

The Phase B engine (v7s8t9u0p1q2) shipped with two Notion buttons
("Opprett oppstartsfaktura" + "Opprett sluttfaktura") and a
fiken_invoice_create task type whose dedup key was
f"{project_page_id}:{invoice_type}". The client revised the design:

  - One Notion button ("Send faktura") with the mode coming from a
    new `Faktura status` select on the Project.
  - The task type is renamed to `send_faktura` and dedups on
    project_page_id alone (only one billable state at a time).
  - `fiken_invoice_lines.fraction` is renamed to `billed_amount_ore` —
    the column now holds the absolute NOK amount billed on this run
    (in øre) rather than a fraction of Pris. Reason: Pris is mutable
    between oppstart and slutt (renegotiation), so a fraction stored
    at oppstart-time would diverge from the actual billed amount once
    Pris changes. The absolute NOK amount is stable.

Schema changes:

  1. Drop the old `uq_sync_tasks_active_fiken_invoice_create` partial
     index. Recreate as `uq_sync_tasks_active_send_faktura`.
  2. Widen `ck_sync_tasks_task_type` to admit `send_faktura` and drop
     `fiken_invoice_create`. Any pending/in-flight `fiken_invoice_create`
     rows are re-typed in place (gmail_thread_id is trimmed of the
     `:oppstart`/`:slutt` suffix). Done rows stay as-is since the CHECK
     constraint also covers historical values; we widen the constraint
     to include BOTH names during the migration window then narrow on
     downgrade.

     Note: PostgreSQL CHECK constraints apply to historical rows too.
     We keep `fiken_invoice_create` in the allowed set so terminal
     (`done`/`failed`) rows from the v1 engine don't violate the
     constraint. The active-task index ensures no NEW rows of that
     type are created.

  3. Rename `fiken_invoice_lines.fraction` to `billed_amount_ore`. The
     column type was Float in v1 (fractions); we ALTER to Integer
     (øre) AND backfill existing rows from `unit_price_ore * fraction`
     (the v1 audit trail captured both, so the conversion is exact).

Revision ID: x9u0v1w2r3s4
Revises: w8t9u0v1q2r3
Create Date: 2026-06-17

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "x9u0v1w2r3s4"
down_revision: str | None = "w8t9u0v1q2r3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Drop the old partial-unique index keyed on the v1 task type.
    op.drop_index(
        "uq_sync_tasks_active_fiken_invoice_create", table_name="sync_tasks"
    )

    # 2. Re-type any pending/in-flight rows from v1 to v2 in place, and
    # trim the `:oppstart`/`:slutt` suffix from gmail_thread_id so the
    # new dedup key (project_page_id alone) matches.
    op.execute(
        """
        UPDATE sync_tasks
        SET task_type = 'send_faktura',
            gmail_thread_id = split_part(gmail_thread_id, ':', 1)
        WHERE task_type = 'fiken_invoice_create'
          AND status IN ('pending','in_progress')
        """
    )

    # 3. Widen the CHECK constraint to admit BOTH names so done/failed
    # rows from the v1 engine remain valid while the new engine writes
    # send_faktura rows.
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
        "'fiken_invoice_create','send_faktura','fiken_poll')",
    )

    # 4. New partial-unique index for the v2 task type.
    op.create_index(
        "uq_sync_tasks_active_send_faktura",
        "sync_tasks",
        ["gmail_thread_id"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('pending','in_progress') AND task_type = 'send_faktura'"
        ),
    )

    # 5. Add `billed_amount_ore` to fiken_invoice_lines, backfill from
    # the v1 (fraction, unit_price_ore) tuple, then drop `fraction`.
    op.add_column(
        "fiken_invoice_lines",
        sa.Column("billed_amount_ore", sa.Integer, nullable=True),
    )
    op.execute(
        """
        UPDATE fiken_invoice_lines
        SET billed_amount_ore = ROUND(fraction * unit_price_ore)::INTEGER
        WHERE billed_amount_ore IS NULL
        """
    )
    op.alter_column(
        "fiken_invoice_lines", "billed_amount_ore", nullable=False
    )
    op.drop_column("fiken_invoice_lines", "fraction")


def downgrade() -> None:
    # Reverse, in order: restore `fraction` from `billed_amount_ore`,
    # drop the v2 index, restore the v1 index + check constraint.
    op.add_column(
        "fiken_invoice_lines",
        sa.Column("fraction", sa.Float, nullable=True),
    )
    # NULLIF guards against division by zero on the few corrupt rows
    # where unit_price_ore might be 0; those become NULL fractions.
    op.execute(
        """
        UPDATE fiken_invoice_lines
        SET fraction = billed_amount_ore::FLOAT / NULLIF(unit_price_ore, 0)
        WHERE fraction IS NULL
        """
    )
    op.alter_column("fiken_invoice_lines", "fraction", nullable=False)
    op.drop_column("fiken_invoice_lines", "billed_amount_ore")

    op.drop_index(
        "uq_sync_tasks_active_send_faktura", table_name="sync_tasks"
    )

    # Re-type any pending v2 rows back to v1 with a re-suffixed
    # gmail_thread_id. We don't know which side (oppstart/slutt) the
    # row would have been, so we guess oppstart — the engine treats
    # this as a recoverable misroute (operator can re-trigger).
    op.execute(
        """
        UPDATE sync_tasks
        SET task_type = 'fiken_invoice_create',
            gmail_thread_id = gmail_thread_id || ':oppstart'
        WHERE task_type = 'send_faktura'
          AND status IN ('pending','in_progress')
        """
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
