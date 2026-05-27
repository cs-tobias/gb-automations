"""Durable work-queue DB API over the `sync_tasks` table.

Pure Postgres operations — no Gmail/Notion imports — so both the webhook
(enqueue) and the worker (claim/mark) depend on this without import cycles.
The queue is the crash-safe record of "threads owed to Notion"; see the
`SyncTask` model docstring for the invariant.

Enqueue is idempotent: a thread that already has an active (pending/in_progress)
row is skipped via the partial unique index, so re-firing on every Gmail push
or reply is a safe no-op.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.db import SessionLocal
from gb_automations.models import SyncTask

logger = logging.getLogger(__name__)

# Predicate for "this row represents work still owed" — used by active-row
# queries (claim, reset, status rollups).
_ACTIVE_PREDICATE = text("status IN ('pending','in_progress')")

# ON CONFLICT predicates must match a partial unique index's WHERE *exactly* for
# Postgres to use it. These mirror uq_sync_tasks_active_thread /
# uq_sync_tasks_active_label in models.py — keep them in lockstep with the index
# definitions (and any migration that changes them).
_ACTIVE_THREAD_PREDICATE = text("status IN ('pending','in_progress') AND task_type = 'thread'")
_ACTIVE_LABEL_PREDICATE = text("status IN ('pending','in_progress') AND task_type = 'label_sync'")
_ACTIVE_NAS_FOLDER_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'nas_folder_sync'"
)
_ACTIVE_TASK_FOLDER_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'task_folder_sync'"
)
_ACTIVE_FRAME_PROJECT_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'frame_project_sync'"
)
_ACTIVE_FRAME_LEVERANSE_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'frame_leveranse_sync'"
)
_ACTIVE_FRAME_COMMENT_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'frame_comment_sync'"
)


async def enqueue_threads(
    user_email: str,
    thread_ids: Iterable[str],
    session: AsyncSession | None = None,
    *,
    rebuild: bool = False,
) -> int:
    """Enqueue threads for syncing. Returns the number of NEW rows inserted.

    Idempotent: a thread with an active (pending/in_progress) row is skipped via
    `uq_sync_tasks_active_thread`. When `session` is passed the insert joins the
    caller's transaction and is NOT committed here (lets the webhook enqueue and
    advance the history cursor atomically); otherwise a private session is
    opened and committed.

    `rebuild=True` flags the task so the worker archives + recreates the thread's
    rows under current code, instead of a plain repair-in-place sync.
    """
    ids = sorted({tid for tid in thread_ids if tid})
    if not ids:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            [
                {
                    "task_type": "thread",
                    "user_email": user_email,
                    "gmail_thread_id": tid,
                    "rebuild": rebuild,
                }
                for tid in ids
            ]
        )
        .on_conflict_do_nothing(
            index_elements=["user_email", "gmail_thread_id"],
            index_where=_ACTIVE_THREAD_PREDICATE,
        )
    )

    if session is not None:
        result = await session.execute(stmt)
        return result.rowcount or 0

    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_label_sync(project_page_id: str) -> int:
    """Enqueue a label-sync task for a project. Returns 1 if newly enqueued, 0 if
    one was already active (idempotent — backs the "Sync to Gmail" button so a
    double-click, or fast clicks on the same project, collapse to one task).

    A label_sync row has no real thread; user_email/gmail_thread_id are NOT-NULL
    placeholders (the dedup index `uq_sync_tasks_active_label` keys on
    project_page_id instead). We stash the page id in gmail_thread_id so the
    worker has it without a second column, and "*" in user_email as a sentinel.
    """
    if not project_page_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="label_sync",
            project_page_id=project_page_id,
            user_email="*",
            gmail_thread_id=project_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["project_page_id"],
            index_where=_ACTIVE_LABEL_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_nas_folder_sync(project_page_id: str) -> int:
    """Enqueue a NAS-folder-sync task for a project. Returns 1 if newly
    enqueued, 0 if one was already active (idempotent — backs the "Sync NAS"
    button so a double-click collapses to one task).

    Mirrors enqueue_label_sync: a nas_folder_sync row has no real thread;
    user_email/gmail_thread_id are NOT NULL placeholders (the dedup index
    `uq_sync_tasks_active_nas_folder` keys on project_page_id instead). We
    stash the page id in gmail_thread_id so the worker has it without a
    second column, and "*" in user_email as a sentinel.
    """
    if not project_page_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="nas_folder_sync",
            project_page_id=project_page_id,
            user_email="*",
            gmail_thread_id=project_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["project_page_id"],
            index_where=_ACTIVE_NAS_FOLDER_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_task_folder_sync(task_page_id: str) -> int:
    """Enqueue a task-folder-sync for a Notion Oppgaver page.

    Like enqueue_label_sync, but keyed on the TASK page id (not the project).
    gmail_thread_id carries the task page id so the worker can dispatch without
    a new column; project_page_id is left NULL — the handler resolves it from
    the task page at process time (and a task can be re-parented without
    rewriting the queue row). Returns 1 if newly enqueued, 0 if a duplicate.
    """
    if not task_page_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="task_folder_sync",
            user_email="*",
            gmail_thread_id=task_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_TASK_FOLDER_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_frame_project_sync(project_page_id: str) -> int:
    """Enqueue a Frame.io project-folder sync for a Notion Project page.

    Mirrors enqueue_label_sync: project_page_id is the dedup key
    (uq_sync_tasks_active_frame_project), user_email/gmail_thread_id are
    placeholders ("*" / project_page_id) so the NOT NULL columns are satisfied.
    Returns 1 if newly enqueued, 0 if a duplicate was already active.
    """
    if not project_page_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="frame_project_sync",
            project_page_id=project_page_id,
            user_email="*",
            gmail_thread_id=project_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["project_page_id"],
            index_where=_ACTIVE_FRAME_PROJECT_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_frame_leveranse_sync(leveranse_page_id: str) -> int:
    """Enqueue a Frame.io leveranse-folder sync for a Notion Leveranser page.

    Mirrors enqueue_task_folder_sync — gmail_thread_id carries the Leveranse
    page id as a placeholder and is the dedup key
    (uq_sync_tasks_active_frame_leveranse). project_page_id is left NULL;
    sync_frame_leveranse resolves the project from the leveranse page at
    process time and the worker calls set_task_project so the Projects-DB
    dot rolls up.
    """
    if not leveranse_page_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="frame_leveranse_sync",
            user_email="*",
            gmail_thread_id=leveranse_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_FRAME_LEVERANSE_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_frame_comment_sync(comment_id: str) -> int:
    """Enqueue processing of one Frame.io comment id.

    Used by the POST /webhooks/frame receiver: the webhook payload only
    carries the comment id (Frame doesn't include the body in webhook
    events — we GET /comments/{id} during processing). Mirrors
    enqueue_frame_leveranse_sync — gmail_thread_id carries the Frame
    comment UUID as a placeholder and is the dedup key
    (uq_sync_tasks_active_frame_comment), so a webhook redelivery while
    the prior task is still pending collapses to one row.

    project_page_id is left NULL; sync_frame_comment resolves it from
    file_id → FrameLeveranseFolder.project_page_id at process time and
    the worker calls set_task_project for the Projects-DB dot.
    """
    if not comment_id:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="frame_comment_sync",
            user_email="*",
            gmail_thread_id=comment_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_FRAME_COMMENT_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


# ============================================================
# Phase 2.5 — status loop enqueues
# ============================================================

_ACTIVE_FRAME_VERSION_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'frame_version_sync'"
)
_ACTIVE_OPPGAVE_DONE_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'oppgave_done_sync'"
)
_ACTIVE_LEVERANSE_STATUS_PREDICATE = text(
    "status IN ('pending','in_progress') AND task_type = 'leveranse_status_recheck'"
)


async def enqueue_frame_version_sync(stack_id: str) -> int:
    """Enqueue processing of a Frame version_stack id after `file.versioned`.

    gmail_thread_id carries the Frame version_stack UUID (the webhook
    event's resource.id when type is file.versioned). Dedup collapses
    rapid-fire deliveries (Frame may fire file.created /
    file.upload.completed / file.ready / file.versioned all close
    together — only file.versioned is acted on; the rest are dropped
    in the receiver).
    """
    if not stack_id:
        return 0
    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="frame_version_sync",
            user_email="*",
            gmail_thread_id=stack_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_FRAME_VERSION_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_oppgave_done_sync(oppgave_page_id: str) -> int:
    """Enqueue propagation of a Notion Korreksjon row's Ferdig checkbox
    to its linked Frame comment.

    gmail_thread_id carries the Notion Oppgave page id. Dedup collapses
    bursts of toggles on the same row (e.g. the team double-clicking) to
    one task — the engine reads the row's current state at process time,
    so collapsing same-id is safe.
    """
    if not oppgave_page_id:
        return 0
    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="oppgave_done_sync",
            user_email="*",
            gmail_thread_id=oppgave_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_OPPGAVE_DONE_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def enqueue_leveranse_status_recheck(leveranse_page_id: str) -> int:
    """Enqueue a rollup recheck for a Leveranse.

    Fired after both propagation engines (sync_frame_comments and
    sync_oppgave_done) write a Korreksjon done-state change. The cascade
    of N children flipping all enqueue the same parent's recheck task;
    the active-dedup index collapses them to one rollup pass.

    gmail_thread_id carries the Leveranse page id.
    """
    if not leveranse_page_id:
        return 0
    stmt = (
        pg_insert(SyncTask)
        .values(
            task_type="leveranse_status_recheck",
            user_email="*",
            gmail_thread_id=leveranse_page_id,
        )
        .on_conflict_do_nothing(
            index_elements=["gmail_thread_id"],
            index_where=_ACTIVE_LEVERANSE_STATUS_PREDICATE,
        )
    )
    async with SessionLocal() as own:
        result = await own.execute(stmt)
        await own.commit()
        return result.rowcount or 0


async def claim_one(session: AsyncSession) -> SyncTask | None:
    """Claim the oldest eligible pending task, marking it in_progress.

    Uses `FOR UPDATE SKIP LOCKED` so concurrent claimers never grab the same row
    and never block each other — correct for the single worker today and for
    multiple workers/processes later with no change. The caller owns the
    transaction (commit on success path).
    """
    stmt = (
        select(SyncTask)
        .where(SyncTask.status == "pending", SyncTask.next_attempt_at <= func.now())
        .order_by(SyncTask.next_attempt_at.asc(), SyncTask.id.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    task = (await session.execute(stmt)).scalar_one_or_none()
    if task is None:
        return None

    task.status = "in_progress"
    task.started_at = datetime.now(UTC)
    task.attempts += 1
    await session.flush()
    return task


async def mark_done(session: AsyncSession, task_id: int) -> None:
    """Mark a task done (synced, or legitimately skipped — e.g. no project)."""
    await session.execute(
        update(SyncTask)
        .where(SyncTask.id == task_id)
        .values(status="done", finished_at=func.now(), last_error=None)
    )


async def mark_failed(
    session: AsyncSession,
    task: SyncTask,
    error: str,
    *,
    max_attempts: int,
    base_backoff_seconds: int,
) -> bool:
    """Record a failed attempt. Returns True if the task is now TERMINALLY failed.

    Below `max_attempts`: reset to pending with exponential backoff so the
    worker retries later. At/above: park as `failed` (stays visible) — the
    caller surfaces this to the client (Notion mirror) and reconcile treats it
    as covered rather than re-enqueuing forever.
    """
    error = (error or "")[:2000]
    terminal = task.attempts >= max_attempts
    if terminal:
        await session.execute(
            update(SyncTask)
            .where(SyncTask.id == task.id)
            .values(status="failed", finished_at=func.now(), last_error=error)
        )
        return True

    # attempts already incremented at claim time; back off 2**(attempts-1).
    delay = base_backoff_seconds * (2 ** max(task.attempts - 1, 0))
    delay = min(delay, 3600)  # cap at 1h
    await session.execute(
        update(SyncTask)
        .where(SyncTask.id == task.id)
        .values(
            status="pending",
            next_attempt_at=datetime.now(UTC) + timedelta(seconds=delay),
            last_error=error,
        )
    )
    return False


async def reset_in_progress() -> int:
    """Boot crash-recovery: flip any in_progress rows back to pending.

    A row left `in_progress` means the process died mid-sync. Re-running is safe
    (sync_thread is idempotent). Returns the number of rows reset.
    """
    async with SessionLocal() as session:
        result = await session.execute(
            update(SyncTask)
            .where(SyncTask.status == "in_progress")
            .values(status="pending", started_at=None, next_attempt_at=func.now())
        )
        await session.commit()
        return result.rowcount or 0


async def requeue_failed(thread_id: str | None = None) -> int:
    """Re-run terminally-failed tasks: flip `failed` rows back to `pending`.

    For operator recovery after fixing the root cause of a failure (e.g. a bad
    Notion DB id). Resets `attempts` to 0 so the thread gets a fresh batch of
    retries, clears the error and the backoff gate. With `thread_id`, requeues
    just that one thread; otherwise every failed task. Returns rows requeued.

    Designed to back a `POST /debug/queue/retry-failed` endpoint (and, later, a
    "Retry" button in the Notion Sync Queue mirror).
    """
    async with SessionLocal() as session:
        stmt = update(SyncTask).where(SyncTask.status == "failed")
        if thread_id:
            stmt = stmt.where(SyncTask.gmail_thread_id == thread_id)
        result = await session.execute(
            stmt.values(
                status="pending",
                attempts=0,
                started_at=None,
                finished_at=None,
                last_error=None,
                next_attempt_at=func.now(),
            )
        )
        await session.commit()
        return result.rowcount or 0


async def set_task_project(task_id: int, project_page_id: str) -> None:
    """Record which Notion project a task resolved to (worker fills this in)."""
    async with SessionLocal() as session:
        await session.execute(
            update(SyncTask).where(SyncTask.id == task_id).values(project_page_id=project_page_id)
        )
        await session.commit()


async def project_sync_state(project_page_id: str) -> str:
    """Return the Projects-DB dot state for a project.

    Precedence (highest first), Deadline-style:
      'failed'   — at least one task terminally stopped (hit max attempts). Red.
      'retrying' — no terminal failures, but an active task has already failed
                   at least once and is still retrying (attempts > 1). Orange:
                   "something's wrong here, but it hasn't given up." Note that a
                   first attempt is attempts == 1 (incremented at claim time), so
                   "has retried" is attempts > 1.
      'active'   — tasks running cleanly, no failures yet. Green.
      'idle'     — nothing pending or failed. Grey.

    Computed straight from sync_tasks so it can't drift from reality (no
    in-memory bookkeeping to get out of sync across restarts).
    """

    async def _exists(session, *conditions) -> bool:
        row = (
            await session.execute(
                select(SyncTask.id)
                .where(SyncTask.project_page_id == project_page_id, *conditions)
                .limit(1)
            )
        ).first()
        return row is not None

    async with SessionLocal() as session:
        if await _exists(session, SyncTask.status == "failed"):
            return "failed"
        if await _exists(
            session,
            SyncTask.status.in_(("pending", "in_progress")),
            SyncTask.attempts > 1,
        ):
            return "retrying"
        if await _exists(session, SyncTask.status.in_(("pending", "in_progress"))):
            return "active"
        return "idle"


async def status_counts() -> dict[str, int]:
    """{status: count} across the whole queue. Cheap; used for log narration."""
    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(SyncTask.status, func.count()).group_by(SyncTask.status)
            )
        ).all()
    return {s: c for s, c in rows}


async def queue_counts(failed_limit: int = 50) -> dict[str, Any]:
    """Snapshot of queue state for the /debug/queue endpoint."""
    async with SessionLocal() as session:
        by_status = dict(
            (
                await session.execute(
                    select(SyncTask.status, func.count()).group_by(SyncTask.status)
                )
            ).all()
        )
        oldest_pending = (
            await session.execute(
                select(func.min(SyncTask.enqueued_at)).where(SyncTask.status == "pending")
            )
        ).scalar_one_or_none()

        in_progress = list(
            (
                await session.execute(
                    select(
                        SyncTask.user_email, SyncTask.gmail_thread_id, SyncTask.started_at
                    ).where(SyncTask.status == "in_progress")
                )
            ).all()
        )
        failed = list(
            (
                await session.execute(
                    select(
                        SyncTask.id,
                        SyncTask.user_email,
                        SyncTask.gmail_thread_id,
                        SyncTask.attempts,
                        SyncTask.last_error,
                        SyncTask.finished_at,
                    )
                    .where(SyncTask.status == "failed")
                    .order_by(SyncTask.finished_at.desc())
                    .limit(failed_limit)
                )
            ).all()
        )

    now = datetime.now(UTC)
    oldest_age = None
    if oldest_pending is not None:
        if oldest_pending.tzinfo is None:
            oldest_pending = oldest_pending.replace(tzinfo=UTC)
        oldest_age = (now - oldest_pending).total_seconds()

    return {
        "counts": {s: by_status.get(s, 0) for s in ("pending", "in_progress", "done", "failed")},
        "oldest_pending_age_seconds": oldest_age,
        "in_progress": [
            {"user_email": e, "gmail_thread_id": t, "started_at": s.isoformat() if s else None}
            for e, t, s in in_progress
        ],
        "failed": [
            {
                "id": i,
                "user_email": e,
                "gmail_thread_id": t,
                "attempts": a,
                "last_error": err,
                "finished_at": f.isoformat() if f else None,
            }
            for i, e, t, a, err, f in failed
        ],
    }


__all__ = [
    "enqueue_threads",
    "enqueue_label_sync",
    "enqueue_task_folder_sync",
    "enqueue_frame_project_sync",
    "enqueue_frame_leveranse_sync",
    "enqueue_frame_comment_sync",
    "enqueue_frame_version_sync",
    "enqueue_oppgave_done_sync",
    "enqueue_leveranse_status_recheck",
    "claim_one",
    "mark_done",
    "mark_failed",
    "reset_in_progress",
    "requeue_failed",
    "set_task_project",
    "project_sync_state",
    "status_counts",
    "queue_counts",
]
