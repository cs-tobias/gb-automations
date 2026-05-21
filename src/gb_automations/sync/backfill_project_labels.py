"""Rebuild the local `project_labels` cache from Notion + Gmail.

The `ProjectLabel` table (Notion project ↔ Gmail label, per user) lives only in
the per-machine Postgres volume, and is normally written *only* by the Notion
"Sync to Gmail" button (`routes/webhooks.py._create_label_for_all_users`). On a
fresh / migrated host that table is empty, so the Gmail push filter
(`watches.fetch_project_label_ids_for_user`) treats every project email as
non-project activity and drops it — the machine is blind until each project's
button is clicked again.

This reconstructs the mapping from the two real sources of truth without
touching Gmail or Notion state: for every Notion project and every active user,
find the *already-existing* Gmail label whose name equals the project's label
path, and upsert the `ProjectLabel` row. Both sides converge on the same string
because the label was minted via `project_label_path(title, created_time)` and
`get_project_pages()` keys are that same path — so exact-name equality is the
correct match, and the year segment is locked to Notion's created_time on both
sides (no ambiguity).

We deliberately do NOT create missing labels here. Minting a label is the
button's job (`_create_label_for_all_users`); a backfill that created them would
mask the real "this mailbox was never set up for this project" signal. Projects
with no matching label land in `no_label` so the operator can click the button.

Idempotent: re-running upserts identical values, and rows already correct are
skipped (counted as `unchanged`) without issuing a write.

Run once after `seed_users` + `start_watches` on a fresh host; see the
`scripts.backfill_project_labels` CLI.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.db import SessionLocal
from gb_automations.models import ProjectLabel, User

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    dry_run: bool
    users: list[str] = field(default_factory=list)
    projects_total: int = 0
    matched: int = 0  # (project, user) pairs whose Gmail label was found
    upserted: int = 0  # rows written (0 in dry-run)
    unchanged: int = 0  # row already had identical id + name — write skipped
    no_label: list[tuple[str, str]] = field(default_factory=list)  # (label_path, user)
    errors: list[str] = field(default_factory=list)


async def _active_users(only_user: str | None) -> list[str]:
    async with SessionLocal() as session:
        emails = list(
            (await session.execute(select(User.email).where(User.active.is_(True)))).scalars()
        )
    if only_user:
        emails = [e for e in emails if e == only_user]
    return emails


async def _existing_rows() -> dict[tuple[str, str], tuple[str, str]]:
    """(notion_page_id, user_email) → (gmail_label_id, current_name), for unchanged-detection."""
    async with SessionLocal() as session:
        rows = list((await session.execute(select(ProjectLabel))).scalars())
    return {(r.notion_page_id, r.user_email): (r.gmail_label_id, r.current_name) for r in rows}


async def backfill_project_labels(
    *, dry_run: bool = False, only_user: str | None = None
) -> BackfillResult:
    """Reconstruct `project_labels` from Notion projects + existing Gmail labels.

    See module docstring. Never creates Gmail labels; never mutates Notion.
    """
    result = BackfillResult(dry_run=dry_run)

    projects = await notion_client.get_project_pages()
    result.projects_total = len(projects)
    users = await _active_users(only_user)
    result.users = users

    if not users:
        result.errors.append("no active users (run seed_users first)")
        logger.warning("↳ no active users — nothing to backfill")
        return result
    if not projects:
        logger.warning("↳ no Notion projects found — nothing to backfill")
        return result

    existing = await _existing_rows()

    # One labels.list per mailbox (not per project) — same batching as create_label.
    labels_by_user: dict[str, dict[str, str]] = {}
    for email in users:
        try:
            labels = await asyncio.to_thread(gmail_client.list_labels, email)
            labels_by_user[email] = {lbl["name"]: lbl["id"] for lbl in labels}
        except Exception as err:  # one bad mailbox must not abort the rest
            logger.exception("↳ could not list labels for %s — skipping mailbox", email)
            result.errors.append(f"{email}: list_labels failed: {err}")

    queued: list[tuple[str, str, str, str]] = []  # (page_id, email, label_id, label_path)
    for label_path, info in sorted(projects.items()):
        page_id = info["id"]
        for email in users:
            by_name = labels_by_user.get(email)
            if by_name is None:  # mailbox errored above
                continue
            label_id = by_name.get(label_path)
            if label_id is None:
                result.no_label.append((label_path, email))
                continue
            result.matched += 1
            if existing.get((page_id, email)) == (label_id, label_path):
                result.unchanged += 1
                continue
            queued.append((page_id, email, label_id, label_path))

    if queued and not dry_run:
        async with SessionLocal() as session:
            for page_id, email, label_id, label_path in queued:
                stmt = (
                    pg_insert(ProjectLabel)
                    .values(
                        notion_page_id=page_id,
                        user_email=email,
                        gmail_label_id=label_id,
                        current_name=label_path,
                    )
                    .on_conflict_do_update(
                        index_elements=["notion_page_id", "user_email"],
                        set_={"gmail_label_id": label_id, "current_name": label_path},
                    )
                )
                await session.execute(stmt)
            await session.commit()
        result.upserted = len(queued)
    elif queued:  # dry-run: report what would be written
        for _, email, _, label_path in queued:
            logger.info("  [DRY RUN] would map %s ↔ %s", label_path, email)

    for label_path, email in result.no_label:
        logger.warning("  ⚠ no Gmail label for %s in %s — click 'Sync to Gmail'", label_path, email)

    logger.info(
        "✅ backfill%s: %d user(s), %d project(s) → %d matched, %d upserted, "
        "%d unchanged, %d unmatched, %d error(s)",
        " [DRY RUN]" if dry_run else "",
        len(users),
        result.projects_total,
        result.matched,
        result.upserted,
        result.unchanged,
        len(result.no_label),
        len(result.errors),
    )
    return result


__all__ = ["BackfillResult", "backfill_project_labels"]
