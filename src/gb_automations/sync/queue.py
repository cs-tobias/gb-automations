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

# Predicate for "this row represents work still owed" — shared by the dedup
# index target and any active-row queries.
_ACTIVE_PREDICATE = text("status IN ('pending','in_progress')")


async def enqueue_threads(
    user_email: str, thread_ids: Iterable[str], session: AsyncSession | None = None
) -> int:
    """Enqueue threads for syncing. Returns the number of NEW rows inserted.

    Idempotent: a thread with an active (pending/in_progress) row is skipped via
    `uq_sync_tasks_active_thread`. When `session` is passed the insert joins the
    caller's transaction and is NOT committed here (lets the webhook enqueue and
    advance the history cursor atomically); otherwise a private session is
    opened and committed.
    """
    ids = sorted({tid for tid in thread_ids if tid})
    if not ids:
        return 0

    stmt = (
        pg_insert(SyncTask)
        .values([{"user_email": user_email, "gmail_thread_id": tid} for tid in ids])
        .on_conflict_do_nothing(
            index_elements=["user_email", "gmail_thread_id"],
            index_where=_ACTIVE_PREDICATE,
        )
    )

    if session is not None:
        result = await session.execute(stmt)
        return result.rowcount or 0

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
    """Return the Projects-DB dot state for a project: 'active' | 'failed' | 'idle'.

    Priority: any pending/in_progress task → active; else any failed task →
    failed; else idle. Computed straight from sync_tasks so it can't drift from
    reality (no in-memory bookkeeping to get out of sync across restarts).
    """
    async with SessionLocal() as session:
        active = (
            await session.execute(
                select(SyncTask.id)
                .where(
                    SyncTask.project_page_id == project_page_id,
                    SyncTask.status.in_(("pending", "in_progress")),
                )
                .limit(1)
            )
        ).first()
        if active:
            return "active"
        failed = (
            await session.execute(
                select(SyncTask.id)
                .where(
                    SyncTask.project_page_id == project_page_id,
                    SyncTask.status == "failed",
                )
                .limit(1)
            )
        ).first()
        return "failed" if failed else "idle"


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
