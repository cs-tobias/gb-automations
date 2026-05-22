"""Rebuild one Notion project's email rows from scratch.

The take-down/re-add engine behind the `resync_project` CLI (and, later, a
Notion button). Used when already-synced data is wrong — a bug fix or a schema
change means the existing rows need rebuilding with the current code.

Sequence for one project (see `resync_project`):
  1. project page id → Gmail label per active user (via ProjectLabel).
  2. enumerate EVERY thread currently under each label (paginated).
  3. threads → Notion page ids, read from the local EmailRow cache (cheap; no
     cross-year Notion query).
  4. archive those Notion pages (trash), then delete the local EmailRow cache so
     the next sync recreates them fresh.
  5. re-run sync_thread once per thread.

Drive files are REUSED, not duplicated: ThreadAttachment is kept by default, so
sync_thread re-links existing Drive files via their stored `drive_links` instead
of re-uploading. `hard=True` clears ThreadAttachment too, forcing fresh uploads
(this duplicates every attachment in Drive — opt-in only).

A failed page-archive is counted, not fatal: the row stays live and the re-sync
re-links it instead of recreating it — degraded but not data-losing.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from googleapiclient.errors import HttpError
from sqlalchemy import delete, select

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.db import SessionLocal
from gb_automations.models import EmailRow, ProjectLabel, ThreadAttachment, User
from gb_automations.sync.queue import enqueue_threads
from gb_automations.sync.sync_thread import SyncResult, sync_thread

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProjectRef:
    """A resolved project: its Notion page id and the label name we know it by."""

    page_id: str
    name: str  # ProjectLabel.current_name, e.g. "Prosjekt/2026/1232_Eiendomsspar_KJ8"


async def list_projects() -> list[ProjectRef]:
    """Every project we have a Gmail-label mapping for, sorted by name.

    Reads `ProjectLabel` — one mapping per project (deduped across users, since
    the page id is shared). This is the menu the CLI prints so a human can pick
    a project by its familiar label name instead of hunting for a page UUID.
    """
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    ProjectLabel.notion_page_id, ProjectLabel.current_name
                ).distinct()
            )
        ).all()
    # Collapse to one ref per page id (multiple users share the same project).
    by_page: dict[str, str] = {}
    for page_id, name in rows:
        by_page.setdefault(page_id, name)
    return sorted(
        (ProjectRef(page_id=p, name=n) for p, n in by_page.items()),
        key=lambda r: r.name.lower(),
    )


async def resolve_project(query: str) -> list[ProjectRef]:
    """Resolve a CLI `--project` value to project(s).

    Accepts either a Notion page id (exact match on `notion_page_id`) or a
    case-insensitive substring of the label name (`current_name`, e.g. "KJ8").
    Returns ALL matches so the caller can detect "none" and "ambiguous" — the
    page-id form returns at most one; a name substring may hit several.
    """
    q = query.strip()
    projects = await list_projects()
    by_id = [p for p in projects if p.page_id == q]
    if by_id:
        return by_id
    ql = q.lower()
    return [p for p in projects if ql in p.name.lower()]

# Bound concurrent Notion archive PATCHes — enough to be quick on a large
# project without tripping Notion's rate limiter (the client already retries).
_ARCHIVE_CONCURRENCY = 6

# Threads currently being resynced, keyed by project page id. Guards against a
# double-click / double-trigger in this single-process deployment. The op is
# largely self-correcting anyway (archive + delete are idempotent, the
# per-thread advisory lock serializes re-create), so this only avoids wasted
# work, not correctness loss.
_in_flight: set[str] = set()


@dataclass
class ResyncResult:
    project_page_id: str
    project_name: str | None = None
    thread_ids: list[str] = field(default_factory=list)
    page_ids_to_archive: list[str] = field(default_factory=list)
    pages_archived: int = 0
    pages_archive_failed: int = 0
    email_rows_deleted: int = 0
    thread_attachments_deleted: int = 0
    rows_created: int = 0
    rows_already_present: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = False
    already_running: bool = False


async def _resolve_labels(
    project_page_id: str, only_user: str | None
) -> list[tuple[str, str]]:
    """Return [(user_email, live_label_name)] for the project's active users.

    Reads ProjectLabel rows for the project, intersected with the active User
    set, then fetches each label's LIVE name from Gmail (by stored id) — robust
    to a Gmail-side rename the same way `_reconcile_label_for_all_users` is.
    """
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ProjectLabel).where(
                        ProjectLabel.notion_page_id == project_page_id
                    )
                )
            )
            .scalars()
            .all()
        )
        active = set(
            (
                await session.execute(select(User.email).where(User.active.is_(True)))
            ).scalars()
        )

    out: list[tuple[str, str]] = []
    for row in rows:
        if only_user and row.user_email != only_user:
            continue
        if row.user_email not in active:
            continue
        try:
            label = await asyncio.to_thread(
                gmail_client.get_label, row.user_email, row.gmail_label_id
            )
        except HttpError:
            logger.warning(
                "↳ label %s for %s not found in Gmail — skipping",
                row.gmail_label_id,
                row.user_email,
            )
            continue
        out.append((row.user_email, label["name"]))
    return out


async def _enumerate_threads(
    labels: list[tuple[str, str]],
) -> dict[str, str]:
    """thread_id → owning user_email, across every label, deduped.

    A thread can be labeled in more than one mailbox; we keep ONE owning user
    per thread (first by sorted email) so each thread is synced exactly once —
    the Notion row is shared across users, so a second sync would only log
    "already present".
    """
    owner: dict[str, str] = {}
    for user_email, label_name in sorted(labels):
        threads = await asyncio.to_thread(
            gmail_client.list_threads_with_label,
            user_email,
            label_name,
            500,
            paginate=True,
        )
        for t in threads:
            owner.setdefault(t["id"], user_email)
    return owner


async def _page_ids_for_threads(thread_ids: list[str]) -> list[str]:
    """Distinct Notion page ids cached locally for these threads."""
    if not thread_ids:
        return []
    async with SessionLocal() as session:
        page_ids = (
            await session.execute(
                select(EmailRow.notion_page_id)
                .where(EmailRow.gmail_thread_id.in_(thread_ids))
                .distinct()
            )
        ).scalars()
    return [pid for pid in page_ids if pid]


async def _archive_pages(page_ids: list[str]) -> tuple[int, int]:
    """Archive each page with bounded concurrency. Returns (ok, failed)."""
    sem = asyncio.Semaphore(_ARCHIVE_CONCURRENCY)
    ok = 0
    failed = 0

    async def _one(pid: str) -> bool:
        async with sem:
            try:
                await notion_client.archive_page(pid)
                return True
            except Exception:
                logger.exception("↳ archive failed for page %s", pid)
                return False

    for result in await asyncio.gather(*(_one(pid) for pid in page_ids)):
        if result:
            ok += 1
        else:
            failed += 1
    return ok, failed


async def _clear_local_cache(thread_ids: list[str], hard: bool) -> tuple[int, int]:
    """Delete EmailRow (always) and ThreadAttachment (hard only) for the threads.

    Returns (email_rows_deleted, thread_attachments_deleted). Keeping
    ThreadAttachment is what lets the re-sync re-link existing Drive files
    instead of re-uploading them.
    """
    if not thread_ids:
        return (0, 0)
    async with SessionLocal() as session:
        er = await session.execute(
            delete(EmailRow).where(EmailRow.gmail_thread_id.in_(thread_ids))
        )
        ta_deleted = 0
        if hard:
            ta = await session.execute(
                delete(ThreadAttachment).where(
                    ThreadAttachment.gmail_thread_id.in_(thread_ids)
                )
            )
            ta_deleted = ta.rowcount or 0
        await session.commit()
        return (er.rowcount or 0, ta_deleted)


async def resync_project(
    project_page_id: str,
    *,
    dry_run: bool = False,
    archive: bool = True,
    hard: bool = False,
    only_user: str | None = None,
) -> ResyncResult:
    """Rebuild every email row for one Notion project. See module docstring.

    - dry_run: enumerate + report what WOULD be archived/synced; mutate nothing.
    - archive: take down (trash) the existing Notion rows before recreating.
      `archive=False` is a repair-only pass — re-run sync on the existing rows.
    - hard: also clear ThreadAttachment, forcing fresh Drive uploads (DUPLICATES
      every attachment in Drive). Default keeps them so files are reused.
    - only_user: restrict to one mailbox's copy of the label.
    """
    result = ResyncResult(project_page_id=project_page_id, dry_run=dry_run)

    if not dry_run and project_page_id in _in_flight:
        logger.warning("↳ resync already running for project %s — skipping", project_page_id)
        result.already_running = True
        return result

    try:
        page = await notion_client.get_page(project_page_id)
        result.project_name = notion_client.extract_page_title(page)
    except Exception:
        logger.exception("↳ could not fetch project page %s (title only)", project_page_id)

    labels = await _resolve_labels(project_page_id, only_user)
    if not labels:
        result.errors.append("no Gmail label mapping for any active user")
        logger.warning(
            "↳ no ProjectLabel for active users on project %s — nothing to resync",
            project_page_id,
        )
        return result

    owner_by_thread = await _enumerate_threads(labels)
    result.thread_ids = sorted(owner_by_thread)
    page_ids = await _page_ids_for_threads(result.thread_ids)
    result.page_ids_to_archive = page_ids

    logger.info(
        "🔁 resync %r (%s): %d thread(s), %d cached Notion row(s)%s",
        result.project_name or project_page_id,
        project_page_id,
        len(result.thread_ids),
        len(page_ids),
        " [DRY RUN]" if dry_run else "",
    )

    if dry_run:
        return result

    _in_flight.add(project_page_id)
    try:
        if archive and page_ids:
            ok, failed = await _archive_pages(page_ids)
            result.pages_archived = ok
            result.pages_archive_failed = failed
            logger.info("  ⌫ archived %d Notion row(s) (%d failed)", ok, failed)

        if archive:
            er, ta = await _clear_local_cache(result.thread_ids, hard)
            result.email_rows_deleted = er
            result.thread_attachments_deleted = ta
            logger.info(
                "  🧹 cleared %d email_row(s)%s",
                er,
                f" + {ta} thread_attachment(s) [HARD — Drive will be re-uploaded]"
                if hard
                else "",
            )

        for thread_id in result.thread_ids:
            owner = owner_by_thread[thread_id]
            try:
                sr: SyncResult = await sync_thread(owner, thread_id)
                result.rows_created += sr.rows_created
                result.rows_already_present += sr.rows_already_present
                result.errors.extend(sr.errors)
            except Exception as err:
                logger.exception("  ✗ sync_thread failed for %s", thread_id)
                result.errors.append(f"{thread_id}: {err}")
    finally:
        _in_flight.discard(project_page_id)

    logger.info(
        "✅ resync done %r: +%d row(s), %d already present, %d error(s)",
        result.project_name or project_page_id,
        result.rows_created,
        result.rows_already_present,
        len(result.errors),
    )
    return result


async def rebuild_thread(thread_id: str, owner_email: str, on_resolved=None) -> SyncResult:
    """Archive + recreate ONE thread's email rows under current code.

    Unlike a plain re-sync (which finds the rows already present and only
    refreshes their project/files), this regenerates every field — body, tags,
    history splitting, attachments — by taking the rows down first, then
    re-running `sync_thread`. Use it after a code change that should re-author
    existing rows.

    Scope is deliberately narrow: it archives only the EMAIL rows for this
    thread and clears only the local `EmailRow` cache. Contacts and Companies
    are NOT deleted — they're re-matched/refreshed by the sync. `ThreadAttachment`
    is kept, so Drive files are re-linked rather than re-uploaded (no duplicates).

    `on_resolved` is forwarded to sync_thread (the worker uses it to backfill the
    queue-mirror subject and light the project dot).
    """
    page_ids = await _page_ids_for_threads([thread_id])
    if page_ids:
        ok, failed = await _archive_pages(page_ids)
        logger.info("🔁 rebuild %s: archived %d row(s) (%d failed)", thread_id, ok, failed)
    await _clear_local_cache([thread_id], hard=False)
    # Rows + cache gone → sync_thread now recreates every row fresh.
    return await sync_thread(owner_email, thread_id, on_resolved=on_resolved)


async def enqueue_project(
    project_page_id: str, *, rebuild: bool = True, only_user: str | None = None
) -> dict[str, int]:
    """Enqueue every thread under a project's label(s) for a (re)sync.

    The queue-based counterpart to `resync_project`: it does NOT archive or sync
    inline. It resolves the project's threads and drops them on the durable
    queue, exactly like the per-email "Re-sync" button does for one thread. The
    queue worker then handles each thread the normal way — with `rebuild=True`
    it archives + recreates the rows under current code — so logging, retries
    and the Projects-DB status dot are all the standard path. This is what the
    Notion "Resync Project" button calls.

    Returns a summary: {threads, enqueued, labels}. `enqueued` < `threads` when
    some threads already have an active queue row (idempotent — those are
    skipped, not duplicated).
    """
    labels = await _resolve_labels(project_page_id, only_user)
    owner_by_thread = await _enumerate_threads(labels)

    by_owner: dict[str, list[str]] = {}
    for thread_id, owner in owner_by_thread.items():
        by_owner.setdefault(owner, []).append(thread_id)

    enqueued = 0
    for owner, thread_ids in by_owner.items():
        enqueued += await enqueue_threads(owner, thread_ids, rebuild=rebuild)

    logger.info(
        "🔁 resync project %s: %d thread(s) across %d label(s) — %d newly enqueued",
        project_page_id,
        len(owner_by_thread),
        len(labels),
        enqueued,
    )
    return {"threads": len(owner_by_thread), "enqueued": enqueued, "labels": len(labels)}


async def enqueue_missing_for_all_projects() -> int:
    """Enqueue every labeled thread that the queue has never finished.

    The boot-time safety net: while the app runs, the durable queue already
    guarantees delivery, but a thread labeled while the app was completely DOWN
    is never enqueued (the push was dropped / fell outside Gmail's history
    window). On startup we enumerate every thread under every project label and
    enqueue any that lack a terminal (`done`/`failed`) row in `sync_tasks`.
    `failed` counts as covered — a terminally-failed thread needs operator
    action, not an automatic daily re-attempt.

    Feeds the same queue as the webhook (one drain path, one retry policy, one
    place to observe). Returns the number of newly-enqueued tasks.
    """
    from gb_automations.models import SyncTask
    from gb_automations.sync.queue import enqueue_threads

    projects = await list_projects()
    if not projects:
        logger.info("reconcile-enqueue: no projects mapped — nothing to do")
        return 0

    total = 0
    for project in projects:
        labels = await _resolve_labels(project.page_id, only_user=None)
        if not labels:
            continue
        owner_by_thread = await _enumerate_threads(labels)
        thread_ids = list(owner_by_thread)
        if not thread_ids:
            continue

        async with SessionLocal() as session:
            covered = set(
                (
                    await session.execute(
                        select(SyncTask.gmail_thread_id).where(
                            SyncTask.gmail_thread_id.in_(thread_ids),
                            SyncTask.status.in_(("done", "failed")),
                        )
                    )
                ).scalars()
            )

        # Group the missing threads by owning mailbox for enqueue.
        missing_by_owner: dict[str, list[str]] = {}
        for tid, owner in owner_by_thread.items():
            if tid not in covered:
                missing_by_owner.setdefault(owner, []).append(tid)

        for owner, tids in missing_by_owner.items():
            inserted = await enqueue_threads(owner, tids)
            total += inserted

    if total:
        logger.info("reconcile-enqueue: enqueued %d previously-missed thread(s)", total)
    else:
        logger.info("reconcile-enqueue: queue already covers every labeled thread")
    return total


__all__ = [
    "ProjectRef",
    "ResyncResult",
    "list_projects",
    "resolve_project",
    "resync_project",
    "rebuild_thread",
    "enqueue_project",
    "enqueue_missing_for_all_projects",
]
