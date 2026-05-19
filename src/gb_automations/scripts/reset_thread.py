"""Reset the local sync cache so threads can be re-synced from scratch.

Use this when iterating on extraction quality / tagging / file upload: delete
the rows in the Notion Emails DB yourself, then run this to clear the matching
local-cache entries. On the next Gmail webhook for the thread (or manual sync),
`sync_thread` will treat it as a first encounter again — history reconstruction
runs, the canonical regex rows are re-created, attachments re-upload, and you
can compare results.

Usage (inside the container):

    # Reset ALL threads (default — handy when you only have 1-2 test projects
    # and want to wipe everything between runs).
    docker compose exec api python -m gb_automations.scripts.reset_thread

    # Reset just one thread:
    docker compose exec api python -m gb_automations.scripts.reset_thread \\
        --thread 19e166c93fdbc5c6

The default also clears `attachment_fingerprints` (signature-detection state),
`emails_db_cache` (the year-partitioned Emails DB router's persistent lookup),
and `contact_cache` + `company_cache` (so contacts/companies get re-resolved
against Notion on the next sync — required when you've manually deleted the
Notion Contacts/Companies rows, otherwise stale Notion page IDs in the cache
will cause Notion PATCH failures). Pass `--keep-fingerprints`, `--keep-db-cache`,
or `--keep-contacts` to preserve any of them across the reset.

Restart the api container after a full reset that includes the DB cache —
the year-DB router also holds an in-process dict cache that survives the
Postgres wipe until the process restarts.

What this does NOT do:
  - Touch Notion. Delete or archive the Notion rows yourself before running.
  - Touch Gmail or Drive. Gmail messages, labels, and Drive-uploaded files
    are unaffected (re-runs will re-upload to Drive; that's fine — new files,
    new Drive entries, no overwrites).
  - Re-trigger a sync. Reapply the label in Gmail to fire a webhook.

Idempotent — running with no cache rows is a silent no-op.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import delete

from gb_automations.db import SessionLocal
from gb_automations.models import (
    AttachmentFingerprint,
    CompanyCache,
    ContactCache,
    EmailRow,
    EmailsDbCache,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("reset_thread")


async def _reset(
    thread_id: str | None,
    keep_fingerprints: bool,
    keep_db_cache: bool,
    keep_contacts: bool,
) -> tuple[int, int, int, int, int]:
    """Delete cached rows. Returns (email_rows, fingerprints, db_cache, contacts, companies)."""
    async with SessionLocal() as session:
        if thread_id:
            email_stmt = delete(EmailRow).where(EmailRow.gmail_thread_id == thread_id)
        else:
            email_stmt = delete(EmailRow)
        email_result = await session.execute(email_stmt)
        email_count = email_result.rowcount or 0

        fp_count = 0
        db_cache_count = 0
        contact_count = 0
        company_count = 0
        # Per-thread resets always keep the workspace-wide caches. Signatures
        # are sender-scoped, the year-DB router state is workspace-wide, and
        # the contact/company caches are keyed by email/domain (also workspace-
        # wide) — clearing any of them on a single-thread reset would affect
        # unrelated threads.
        if thread_id is None:
            if not keep_fingerprints:
                fp_result = await session.execute(delete(AttachmentFingerprint))
                fp_count = fp_result.rowcount or 0
            if not keep_db_cache:
                db_result = await session.execute(delete(EmailsDbCache))
                db_cache_count = db_result.rowcount or 0
            if not keep_contacts:
                # Order matters: contact_cache rows reference company_cache
                # rows indirectly (Notion-side relation). Local rows have no
                # FK so we could delete in any order, but doing contacts
                # first mirrors the upsert order in sync_thread.
                contact_result = await session.execute(delete(ContactCache))
                contact_count = contact_result.rowcount or 0
                company_result = await session.execute(delete(CompanyCache))
                company_count = company_result.rowcount or 0

        await session.commit()
        return email_count, fp_count, db_cache_count, contact_count, company_count


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Clear local sync cache. With no --thread, wipes ALL email_rows + "
            "attachment_fingerprints (handy for testing). Notion is left alone."
        ),
    )
    parser.add_argument(
        "--thread",
        default=None,
        help=(
            "Gmail thread ID (16-char hex from sync logs). When omitted, "
            "ALL threads are reset."
        ),
    )
    parser.add_argument(
        "--keep-fingerprints",
        action="store_true",
        help=(
            "On a full reset, preserve the attachment_fingerprints table "
            "(learned signature images). Default is to wipe it so the next "
            "sync starts with fresh signature-detection state."
        ),
    )
    parser.add_argument(
        "--keep-db-cache",
        action="store_true",
        help=(
            "On a full reset, preserve the emails_db_cache table (the "
            "year-partitioned Emails DB router's persistent lookup). Default "
            "is to wipe it so the next sync re-resolves year DBs from Notion."
        ),
    )
    parser.add_argument(
        "--keep-contacts",
        action="store_true",
        help=(
            "On a full reset, preserve the contact_cache + company_cache "
            "tables. Default is to wipe both so the next sync re-resolves "
            "contacts/companies against Notion — required when you've "
            "manually deleted the Notion Contacts/Companies rows, otherwise "
            "stale notion_page_id values in the cache cause PATCH failures."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    email_count, fp_count, db_cache_count, contact_count, company_count = asyncio.run(
        _reset(
            args.thread,
            args.keep_fingerprints,
            args.keep_db_cache,
            args.keep_contacts,
        )
    )

    if args.thread:
        if email_count:
            logger.info(
                "Cleared %d cache row(s) for thread %s. "
                "Reapply the Gmail label to re-sync.",
                email_count,
                args.thread,
            )
        else:
            logger.warning(
                "No cache rows found for thread %s — already clear, or wrong thread ID.",
                args.thread,
            )
        return

    # Full reset
    if (
        email_count == 0
        and fp_count == 0
        and db_cache_count == 0
        and contact_count == 0
        and company_count == 0
    ):
        logger.warning("Nothing to clear — cache is already empty.")
        return
    parts = [f"{email_count} email_row(s)"]
    if fp_count:
        parts.append(f"{fp_count} attachment_fingerprint(s)")
    elif args.keep_fingerprints:
        parts.append("(fingerprints kept)")
    if db_cache_count:
        parts.append(f"{db_cache_count} emails_db_cache row(s)")
    elif args.keep_db_cache:
        parts.append("(db cache kept)")
    if contact_count or company_count:
        parts.append(
            f"{contact_count} contact_cache + {company_count} company_cache row(s)"
        )
    elif args.keep_contacts:
        parts.append("(contacts/companies kept)")
    logger.info(
        "Full reset complete: cleared %s. Reapply Gmail labels to re-sync.",
        ", ".join(parts),
    )
    # The year-DB router holds an in-process dict cache that survives the
    # Postgres wipe. Restart is required for the API process to forget it.
    if db_cache_count:
        logger.info(
            "Restart the api container so the in-process DB-router cache "
            "drops too: docker compose restart api"
        )


if __name__ == "__main__":
    main()
