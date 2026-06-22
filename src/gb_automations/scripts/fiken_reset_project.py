"""One-shot cleanup for a project's tangled Fiken draft state.

The send_faktura engine could create DUPLICATE Fiken drafts when a run
crashed after the draft POST but before the audit row was durable (the
historical NULL-discipline crash retried from the top each time, minting a
fresh draft on every attempt). The split-transaction fix in
sync_fiken_invoice.create_fiken_invoice prevents NEW duplicates, but a
project that already collected orphan / stale drafts needs a manual sweep.

This CLI reuses the same engine functions the /debug/fiken/* endpoints
call. By default it does two things, scoped to ONE project:

  1. reset_project_drafts — for every FikenInvoice row of this project
     with sent_at IS NULL: best-effort DELETE the live Fiken draft and
     stamp sent_at so it drops out of the slutt remainder math and the
     same-mode block check. (These are drafts we KNOW about — they have an
     audit row.)

  2. find_orphan_drafts (scoped by ourReference == project title) — drafts
     live in Fiken with NO audit row at all (created by a crashed run
     before it recorded anything, or by hand in Fiken's UI). With
     --delete-orphans, each is deleted too.

It NEVER touches Notion billing columns (Fakturert beløp / Fakturert
status). Those are the operator's to set and graduation owns them; this
only clears the phantom DRAFT state.

Usage (inside the container):

    # Dry run — report what would be cleared, change nothing:
    docker compose exec api python -m gb_automations.scripts.fiken_reset_project \\
        --project 38057c8b-a80e-8025-aa09-f022208e2977 --dry-run

    # Clear the known (audit-backed) drafts for the project:
    docker compose exec api python -m gb_automations.scripts.fiken_reset_project \\
        --project 38057c8b-a80e-8025-aa09-f022208e2977

    # Also delete orphan drafts (no audit row) matched by ourReference:
    docker compose exec api python -m gb_automations.scripts.fiken_reset_project \\
        --project 38057c8b-a80e-8025-aa09-f022208e2977 --delete-orphans

Idempotent — re-running after a clean sweep is a silent no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from gb_automations.clients import notion as notion_client
from gb_automations.config import settings

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("fiken_reset_project")


async def _run(
    project_page_id: str, *, dry_run: bool, delete_orphans: bool
) -> None:
    # Imported here so the module imports cleanly even if the engine's
    # heavier deps aren't needed for --help.
    from gb_automations.sync.sync_fiken_invoice import (
        delete_draft_and_mark_stale,
        find_orphan_drafts,
        reset_project_drafts,
    )

    company_slug = settings.fiken_company_slug
    if not company_slug:
        logger.error("FIKEN_COMPANY_SLUG is not set — nothing to do.")
        return

    project_title: str | None = None
    try:
        project_page = await notion_client.get_page(project_page_id)
        project_title = (
            notion_client.extract_page_title(project_page) or ""
        ).strip() or None
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "Could not read project %s title (%s) — orphan matching by "
            "ourReference will be skipped.",
            project_page_id,
            err,
        )

    # Always REPORT orphans (read-only) so the operator sees them even on
    # a non-orphan run.
    orphans = await find_orphan_drafts(company_slug, project_title=project_title)
    if orphans:
        logger.info(
            "Found %d orphan draft(s) in Fiken with no audit row "
            "(matched by ourReference == %r):",
            len(orphans),
            project_title,
        )
        for o in orphans:
            logger.info(
                "  orphan draft_id=%s reference=%r issue_date=%s",
                o["draft_id"],
                o.get("reference"),
                o.get("issue_date"),
            )
    else:
        logger.info("No orphan drafts found for this project.")

    if dry_run:
        # Report the audit-backed drafts that WOULD be reset, without
        # touching Fiken or Postgres.
        from sqlalchemy import select

        from gb_automations.db import SessionLocal
        from gb_automations.models import FikenInvoice

        async with SessionLocal() as session:
            unsent = list(
                (
                    await session.execute(
                        select(FikenInvoice.fiken_invoice_id).where(
                            FikenInvoice.company_slug == company_slug,
                            FikenInvoice.project_page_id == project_page_id,
                            FikenInvoice.sent_at.is_(None),
                        )
                    )
                ).scalars()
            )
        logger.info(
            "[dry-run] would reset %d audit-backed unsent draft(s): %s",
            len(unsent),
            ", ".join(unsent) or "(none)",
        )
        if delete_orphans and orphans:
            logger.info(
                "[dry-run] would also delete %d orphan draft(s).", len(orphans)
            )
        logger.info("[dry-run] no changes made.")
        return

    # 1. Reset the known (audit-backed) drafts.
    summary = await reset_project_drafts(company_slug, project_page_id)
    logger.info(
        "Reset %d audit-backed unsent draft(s): %s",
        summary["unsent_audit_rows"],
        ", ".join(
            f"{d['draft_id']}→{d['outcome']}"
            for d in summary["drafts_processed"]
        )
        or "(none)",
    )

    # 2. Optionally delete orphans (no audit row).
    if delete_orphans and orphans:
        for o in orphans:
            outcome = await delete_draft_and_mark_stale(
                company_slug, o["draft_id"]
            )
            logger.info("Orphan draft %s → %s", o["draft_id"], outcome)
    elif orphans:
        logger.info(
            "Left %d orphan draft(s) in place — re-run with --delete-orphans "
            "to remove them.",
            len(orphans),
        )

    logger.info(
        "Done. Re-run /debug/fiken/inspect to confirm the project reads "
        "clean, then re-click Send faktura."
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clear a project's tangled Fiken draft state (duplicate / orphan "
            "drafts) so a fresh Send faktura click starts clean. Never touches "
            "Notion billing columns."
        ),
    )
    parser.add_argument(
        "--project",
        required=True,
        help="Notion Project page id.",
    )
    parser.add_argument(
        "--delete-orphans",
        action="store_true",
        help=(
            "Also delete orphan drafts (live in Fiken, no audit row) matched "
            "to this project by ourReference == project title. Off by default "
            "so an unattributable hand-made draft is never removed silently."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be cleared without changing anything.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    asyncio.run(
        _run(
            args.project,
            dry_run=args.dry_run,
            delete_orphans=args.delete_orphans,
        )
    )


if __name__ == "__main__":
    main()
