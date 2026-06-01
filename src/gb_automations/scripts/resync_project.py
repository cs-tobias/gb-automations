"""Rebuild every email row for one Notion project, on demand.

Take down (archive) the project's existing Notion email rows and re-create them
by re-running the sync over every Gmail thread currently under the project's
label — so the rows get rebuilt with the current code. Use after a bug fix or a
schema change that needs already-synced data refreshed.

Usage (inside the container):

    # List the projects you can resync (by their familiar label name):
    docker compose exec api python -m gb_automations.scripts.resync_project --list

    # See what it would do — no changes (match a project by name substring):
    docker compose exec api python -m gb_automations.scripts.resync_project \\
        --project KJ8 --dry-run

    # Rebuild the project (archive old rows, recreate fresh, reuse Drive files):
    docker compose exec api python -m gb_automations.scripts.resync_project \\
        --project KJ8

`--project` accepts EITHER a Notion page id (the UUID from the project page URL)
OR a case-insensitive substring of the project's label name (e.g. "KJ8" matches
"Prosjekt/2026/1232_Eiendomsspar_KJ8"). If the substring matches more than one
project, the command lists the matches and exits so you can be more specific.
Run `--list` to see every resyncable project.

Flags:
  --dry-run      Enumerate threads + list the Notion rows that would be archived;
                 mutate nothing.
  --no-archive   Repair-only: re-run the sync over existing rows WITHOUT taking
                 them down first. Won't fix structural row changes, but is cheap.
  --user EMAIL   Restrict to one mailbox's copy of the project label.

The project page id is the Notion page UUID — copy it from the project page URL
(the 32-hex chunk), or from a `[notion:...]`-tagged sync log line.

Idempotent: archiving an archived page is a no-op, and re-running produces the
same rows.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from gb_automations.sync.resync_project import (
    ResyncResult,
    list_projects,
    resolve_project,
    resync_project,
)

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("resync_project")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Rebuild all Notion email rows for one project by re-running the "
            "sync over every Gmail thread under its label."
        ),
    )
    parser.add_argument(
        "--project",
        default=None,
        help=(
            "Notion page id (UUID) OR a substring of the project's label name "
            "(e.g. 'KJ8'). Required unless --list is given."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List every resyncable project (by label name) and exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be archived/synced without changing anything.",
    )
    parser.add_argument(
        "--no-archive",
        action="store_true",
        help="Repair-only: re-sync existing rows without taking them down first.",
    )
    parser.add_argument(
        "--user",
        default=None,
        help="Restrict to one mailbox's copy of the project label.",
    )
    return parser.parse_args()


def _summarize(r: ResyncResult, *, dry_run: bool, no_archive: bool) -> None:
    if r.already_running:
        logger.warning("A resync for this project is already running — skipped.")
        return
    if r.errors and not r.thread_ids:
        logger.warning("Nothing to do: %s", "; ".join(r.errors))
        return

    name = r.project_name or r.project_page_id
    if dry_run:
        logger.info(
            "[DRY RUN] %r: %d thread(s) under label, %d Notion row(s) would be archived.",
            name,
            len(r.thread_ids),
            len(r.page_ids_to_archive),
        )
        return

    parts = [f"+{r.rows_created} row(s)", f"{r.rows_already_present} already present"]
    if not no_archive:
        parts.insert(0, f"archived {r.pages_archived}")
        if r.pages_archive_failed:
            parts.append(f"{r.pages_archive_failed} archive-failure(s)")
    if r.errors:
        parts.append(f"{len(r.errors)} error(s)")
    logger.info("Resync %r complete: %s.", name, ", ".join(parts))
    for err in r.errors:
        logger.warning("  • %s", err)


async def _run(args: argparse.Namespace) -> int:
    """Resolve --project then run. Returns a process exit code."""
    if args.list:
        projects = await list_projects()
        if not projects:
            logger.warning("No projects mapped yet — click 'Sync to Gmail' on a project first.")
            return 1
        logger.info("Resyncable projects (%d):", len(projects))
        for p in projects:
            logger.info("  %s   [%s]", p.name, p.page_id)
        return 0

    if not args.project:
        logger.error("Pass --project <id-or-name>, or --list to see the options.")
        return 2

    matches = await resolve_project(args.project)
    if not matches:
        logger.error(
            "No project matches %r. Run --list to see resyncable projects.",
            args.project,
        )
        return 2
    if len(matches) > 1:
        logger.error("%r is ambiguous — matches %d projects:", args.project, len(matches))
        for p in matches:
            logger.error("  %s   [%s]", p.name, p.page_id)
        logger.error("Re-run with a more specific name or the page id.")
        return 2

    target = matches[0]
    logger.info("→ Resolved %r → %s [%s]", args.project, target.name, target.page_id)
    result = await resync_project(
        target.page_id,
        dry_run=args.dry_run,
        archive=not args.no_archive,
        only_user=args.user,
    )
    # The engine couldn't read the title pre-resolution; show the label name.
    result.project_name = result.project_name or target.name
    _summarize(result, dry_run=args.dry_run, no_archive=args.no_archive)
    return 0


def main() -> None:
    args = _parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
