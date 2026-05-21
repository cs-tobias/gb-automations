"""Re-run terminally-failed sync tasks after fixing their root cause.

When a thread exhausts its retries it parks as `failed` (visible in
/debug/queue and the Notion Sync Queue mirror) rather than looping forever.
Once you've fixed the underlying problem — e.g. shared the right Notion DB with
the integration, or corrected a property name — run this to flip those rows
back to `pending` so the worker re-syncs them with a fresh attempt budget.

Usage (inside the container):

    # Retry every failed task:
    docker compose exec api python -m gb_automations.scripts.retry_failed

    # Retry just one thread:
    docker compose exec api python -m gb_automations.scripts.retry_failed --thread 19e445be9beead31

Equivalent to POST /debug/queue/retry-failed; this is the CLI form for ops.
Idempotent: with nothing failed, it reports 0 and does nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from gb_automations.sync.queue import requeue_failed

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("retry_failed")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Requeue terminally-failed sync tasks.")
    parser.add_argument(
        "--thread",
        default=None,
        help="Gmail thread id to retry. Omit to retry all failed tasks.",
    )
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    requeued = await requeue_failed(args.thread)
    if requeued:
        logger.info("Requeued %d failed task(s) → pending; the worker will re-sync them.", requeued)
    else:
        scope = f" for {args.thread}" if args.thread else ""
        logger.info("Nothing to requeue (no failed tasks%s).", scope)
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
