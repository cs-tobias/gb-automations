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

The default also clears the `attachment_fingerprints` table so the next sync
gets fresh signature-detection state — every image gets re-evaluated. Pass
`--keep-fingerprints` to preserve learned signatures across the reset (rare;
useful if you specifically want to test that previously-learned signatures
stay classified).

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
from gb_automations.models import AttachmentFingerprint, EmailRow

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("reset_thread")


async def _reset(thread_id: str | None, keep_fingerprints: bool) -> tuple[int, int]:
    """Delete cached rows. Returns (email_rows_deleted, fingerprints_deleted)."""
    async with SessionLocal() as session:
        if thread_id:
            email_stmt = delete(EmailRow).where(EmailRow.gmail_thread_id == thread_id)
        else:
            email_stmt = delete(EmailRow)
        email_result = await session.execute(email_stmt)
        email_count = email_result.rowcount or 0

        fp_count = 0
        # Only the no-thread (full reset) path clears fingerprints by default.
        # Per-thread resets always keep them because signatures are sender-
        # scoped, not thread-scoped — clearing them on a single-thread reset
        # would affect unrelated other threads from the same senders.
        if thread_id is None and not keep_fingerprints:
            fp_result = await session.execute(delete(AttachmentFingerprint))
            fp_count = fp_result.rowcount or 0

        await session.commit()
        return email_count, fp_count


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
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    email_count, fp_count = asyncio.run(_reset(args.thread, args.keep_fingerprints))

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
    if email_count == 0 and fp_count == 0:
        logger.warning("Nothing to clear — cache is already empty.")
        return
    parts = [f"{email_count} email_row(s)"]
    if fp_count:
        parts.append(f"{fp_count} attachment_fingerprint(s)")
    elif args.keep_fingerprints:
        parts.append("(fingerprints kept)")
    logger.info(
        "Full reset complete: cleared %s. Reapply Gmail labels to re-sync.",
        ", ".join(parts),
    )


if __name__ == "__main__":
    main()
