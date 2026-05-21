"""One-off: rebuild the local `project_labels` cache from Notion + Gmail.

The (project ↔ Gmail label, per user) mapping lives only in the per-machine
Postgres volume and is normally written only by the Notion "Sync to Gmail"
button. On a fresh / migrated host it's empty, so the Gmail push filter drops
every project email. This reconstructs it from the two sources of truth without
creating any labels or touching Notion. See
`gb_automations.sync.backfill_project_labels` for the engine and the rationale.

Run after `seed_users` and `start_watches`, then run `reconcile` to pull in any
threads that were labeled while the host was down.

Usage (inside the container):

    # See what would be mapped — changes nothing:
    docker compose exec api python -m gb_automations.scripts.backfill_project_labels --dry-run

    # Write the mapping rows:
    docker compose exec api python -m gb_automations.scripts.backfill_project_labels

    # Restrict to one mailbox:
    docker compose exec api python -m gb_automations.scripts.backfill_project_labels --user post@goldbox.no
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from gb_automations.sync.backfill_project_labels import BackfillResult, backfill_project_labels

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("backfill_project_labels")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild project_labels from Notion projects + existing Gmail labels.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be mapped without writing any rows.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Restrict to one mailbox.",
    )
    return parser.parse_args()


def _summarize(r: BackfillResult) -> None:
    verb = "would map" if r.dry_run else "mapped"
    logger.info(
        "%s %d row(s) (%d unchanged) across %d user(s), %d project(s); %d unmatched.",
        verb,
        r.upserted if not r.dry_run else r.matched - r.unchanged,
        r.unchanged,
        len(r.users),
        r.projects_total,
        len(r.no_label),
    )
    if r.no_label:
        logger.info("Projects with no Gmail label yet (click 'Sync to Gmail' on these):")
        for label_path, email in r.no_label:
            logger.info("  • %s  [%s]", label_path, email)
    for err in r.errors:
        logger.warning("  ! %s", err)


async def _run(args: argparse.Namespace) -> int:
    result = await backfill_project_labels(dry_run=args.dry_run, only_user=args.user)
    _summarize(result)
    # Hard failure only when nothing could run at all (e.g. no seeded users).
    if not result.users:
        return 1
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
