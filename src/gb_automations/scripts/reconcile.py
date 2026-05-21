"""Sync any threads sitting under a project label that Notion never received.

The Gmail push only ever syncs the threads named in a single historyId delta. So
a thread labeled while this host was DOWN (or before the project was mapped on
this host) moves past the watermark and is *permanently invisible* — syncing a
later thread does not rediscover it. This is silent data loss by omission.

Reconcile closes that gap: for each project it enumerates EVERY thread currently
under the label and re-runs the sync, so threads missing from Notion get created
while threads already present log "already present" (Notion-backed dedup, so no
duplicates). It is the repair-only path of `resync_project` (archive=False) — it
never takes down existing rows.

Run this after any downtime, and as the last step of a fresh-host bootstrap
(after `backfill_project_labels`, which it depends on to know the labels).

Idempotent and safe to re-run: a second pass reports everything already present.

Usage (inside the container):

    # Reconcile every mapped project:
    docker compose exec api python -m gb_automations.scripts.reconcile

    # Preview without changing anything:
    docker compose exec api python -m gb_automations.scripts.reconcile --dry-run

    # One project (page id or a substring of its label name, e.g. "KJ8"):
    docker compose exec api python -m gb_automations.scripts.reconcile --project KJ8

    # Restrict to one mailbox's copy of the label(s):
    docker compose exec api python -m gb_automations.scripts.reconcile --user post@goldbox.no
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from gb_automations.sync.resync_project import (
    ProjectRef,
    ResyncResult,
    list_projects,
    resolve_project,
    resync_project,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("reconcile")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sync threads sitting under a project label that are missing from "
            "Notion (e.g. labeled while the host was down). Repair-only — never "
            "archives existing rows."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help="Limit to one project: Notion page id OR a substring of its label name.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List threads that would be synced without changing anything.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Restrict to one mailbox's copy of the label(s).",
    )
    return parser.parse_args()


def _summarize_one(r: ResyncResult, *, name: str, dry_run: bool) -> None:
    if r.already_running:
        logger.warning("  %s: a resync/reconcile is already running — skipped.", name)
        return
    if dry_run:
        logger.info("  [DRY RUN] %s: %d thread(s) under label.", name, len(r.thread_ids))
        return
    parts = [f"+{r.rows_created} recovered", f"{r.rows_already_present} already present"]
    if r.errors:
        parts.append(f"{len(r.errors)} error(s)")
    logger.info("  %s: %s.", name, ", ".join(parts))
    for err in r.errors:
        logger.warning("    • %s", err)


async def _targets(args: argparse.Namespace) -> list[ProjectRef] | None:
    """Resolve which projects to reconcile. None signals a fatal resolution error."""
    if not args.project:
        projects = await list_projects()
        if not projects:
            logger.error(
                "No projects mapped — run backfill_project_labels first "
                "(or click 'Sync to Gmail' on a project)."
            )
            return None
        return projects

    matches = await resolve_project(args.project)
    if not matches:
        logger.error("No project matches %r. Run without --project to reconcile all.", args.project)
        return None
    if len(matches) > 1:
        logger.error("%r is ambiguous — matches %d projects:", args.project, len(matches))
        for p in matches:
            logger.error("  %s   [%s]", p.name, p.page_id)
        return None
    return matches


async def _run(args: argparse.Namespace) -> int:
    targets = await _targets(args)
    if targets is None:
        return 2

    logger.info(
        "Reconciling %d project(s)%s%s…",
        len(targets),
        " [DRY RUN]" if args.dry_run else "",
        f" for {args.user}" if args.user else "",
    )
    total_recovered = 0
    total_errors = 0
    for target in targets:
        result = await resync_project(
            target.page_id,
            dry_run=args.dry_run,
            archive=False,  # repair-only: never take down existing rows
            only_user=args.user,
        )
        result.project_name = result.project_name or target.name
        _summarize_one(result, name=result.project_name, dry_run=args.dry_run)
        total_recovered += result.rows_created
        total_errors += len(result.errors)

    if not args.dry_run:
        logger.info(
            "✅ reconcile done: %d row(s) recovered across %d project(s), %d error(s).",
            total_recovered,
            len(targets),
            total_errors,
        )
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
