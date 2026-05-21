"""Background worker that drains the durable sync queue (`sync_tasks`).

The Gmail webhook only enqueues rows; this worker is what actually guarantees
each thread reaches Notion. It runs for the life of the process (started in
main.py's lifespan), claiming the oldest eligible `pending` task, running
`sync_thread`, and recording the outcome with retry/backoff. A terminally
failed task is parked as `failed` and surfaced to the client via the Notion
mirror (see `sync.queue_mirror`).

Concurrency is a single constant. Today CONCURRENCY=1 reproduces the previous
serial behavior (one thread synced at a time). Raising it spawns N independent
consumer loops sharing the same claim query — `FOR UPDATE SKIP LOCKED` and the
per-thread advisory lock in sync_thread make that safe with no other change.

Latency: `wake()` (called by the webhook right after it commits an enqueue)
sets an event so an idle consumer picks the work up immediately. A periodic
poll is the fallback that also re-checks backoff-eligible retries.
"""

from __future__ import annotations

import asyncio
import logging

from gb_automations.db import SessionLocal
from gb_automations.obs import describe_error, log_api_error
from gb_automations.sync import queue_mirror
from gb_automations.sync.queue import (
    claim_one,
    mark_done,
    mark_failed,
    set_task_project,
    status_counts,
)
from gb_automations.sync.sync_thread import sync_thread

logger = logging.getLogger(__name__)

CONCURRENCY = 1
MAX_ATTEMPTS = 5
BASE_BACKOFF_SECONDS = 30
POLL_INTERVAL_SECONDS = 15.0

_wake_event = asyncio.Event()
_shutdown = False


def wake() -> None:
    """Nudge idle consumers to check the queue now (called after an enqueue)."""
    _wake_event.set()


def request_shutdown() -> None:
    """Signal the worker to stop after in-flight tasks settle."""
    global _shutdown
    _shutdown = True
    _wake_event.set()


async def _claim() -> tuple[int, str, str, int] | None:
    """Atomically claim the next task. Returns (id, email, thread_id, attempts)."""
    async with SessionLocal() as session:
        task = await claim_one(session)
        if task is None:
            return None
        await session.commit()
        return (task.id, task.user_email, task.gmail_thread_id, task.attempts)


async def _process(claimed: tuple[int, str, str, int], progress: str) -> None:
    """Run sync_thread for an already-claimed task and record the outcome.

    `progress` is a human "N/M" batch position used purely for log narration.
    The claim already committed `in_progress` (so boot recovery sees a mid-sync
    crash); the sync and its outcome run here in separate transactions.
    """
    task_id, email, thread_id, attempts = claimed

    retry_note = f" (retry {attempts}/{MAX_ATTEMPTS})" if attempts > 1 else ""
    logger.info("▶ task %s — syncing thread %s%s", progress, thread_id, retry_note)

    # Subject + project aren't known until the sync fetches the thread; show the
    # thread id as a placeholder, then (via on_resolved, ~1-2s in) backfill the
    # row's subject, persist the resolved project on the task, and light the
    # project's 🟢 dot mid-flight. All mirror writes are best-effort.
    await queue_mirror.mark_processing(thread_id, subject=thread_id)
    resolved_project: str | None = None

    async def _on_resolved(subject: str, project_page_id: str | None) -> None:
        nonlocal resolved_project
        resolved_project = project_page_id
        await queue_mirror.mark_processing(thread_id, subject=subject)
        if project_page_id:
            await set_task_project(task_id, project_page_id)
            await queue_mirror.refresh_project_dot(project_page_id)

    try:
        result = await sync_thread(email, thread_id, on_resolved=_on_resolved)
        # result.errors entries can stringify to "" (e.g. an httpx ReadTimeout),
        # which would make the failure reason invisible — describe_error keeps
        # every entry meaningful (falls back to the exception class name).
        outcome_error = "; ".join(e for e in result.errors if e) or None if result.errors else None
    except Exception as err:
        log_api_error(logger, f"sync_thread crashed for {thread_id}", err)
        outcome_error = describe_error(err)
        result = None

    async with SessionLocal() as session:
        if outcome_error is None:
            await mark_done(session, task_id)
            await session.commit()
            await queue_mirror.remove(thread_id)
            subject = (getattr(result, "thread_subject", None) or "").strip()
            logger.info(
                "✔ task %s done%s",
                progress,
                f" — {subject!r}" if subject else "",
            )
        else:
            # Re-attach a lightweight object carrying the post-claim attempts so
            # mark_failed can decide terminal vs retry without re-querying.
            stub = _TaskStub(id=task_id, attempts=attempts)
            terminal = await mark_failed(
                session,
                stub,
                outcome_error,
                max_attempts=MAX_ATTEMPTS,
                base_backoff_seconds=BASE_BACKOFF_SECONDS,
            )
            await session.commit()
            if terminal:
                logger.error(
                    "✖ task %s FAILED for good after %d attempts (thread %s): %s",
                    progress,
                    attempts,
                    thread_id,
                    outcome_error,
                )
                await queue_mirror.mark_failed(
                    thread_id, subject=thread_id, error=outcome_error, result=result
                )
            else:
                logger.warning(
                    "↻ task %s failed (attempt %d/%d) — will retry: %s",
                    progress,
                    attempts,
                    MAX_ATTEMPTS,
                    outcome_error,
                )

    # Recompute the project dot now this task settled: 🟢 if siblings are still
    # running, 🔴 if it (or a sibling) is failed, ⚪ idle once nothing remains.
    # Prefer the project resolved this run; fall back to whatever was persisted.
    project = resolved_project or getattr(result, "project_page_id", None)
    await queue_mirror.refresh_project_dot(project)


class _TaskStub:
    """Minimal stand-in for a SyncTask row, carrying the fields mark_failed reads."""

    __slots__ = ("id", "attempts")

    def __init__(self, id: int, attempts: int) -> None:  # noqa: A002 - matches column name
        self.id = id
        self.attempts = attempts


async def _pending_now() -> int:
    """How many tasks are still waiting (claimable now or after backoff)."""
    return (await status_counts()).get("pending", 0)


async def _consumer(name: int) -> None:
    """One consumer loop: drain the queue (narrating N/M), then wait or poll.

    The denominator M is recomputed each task as `done-so-far + still-pending`,
    so it grows to reflect work that arrives mid-drain. With one task claimed
    and three still pending, the first task reads `1/4`; if nothing new arrives
    it counts 1/4 → 2/4 → 3/4 → 4/4. New arrivals push M up naturally.
    """
    batch_pos = 0  # N: tasks started this batch (0 = idle, between batches)
    while not _shutdown:
        claimed = await _claim()

        if claimed is None:
            # Queue empty: close out the batch narration if we were in one.
            if batch_pos:
                logger.info("📭 queue drained — %d processed", batch_pos)
            batch_pos = 0
            try:
                await asyncio.wait_for(_wake_event.wait(), timeout=POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass
            _wake_event.clear()
            continue

        # Live total = this task (just claimed) + everything still pending.
        remaining = await _pending_now()
        batch_pos += 1
        total = batch_pos + remaining
        if batch_pos == 1:  # idle → busy: announce the batch
            failed = (await status_counts()).get("failed", 0)
            logger.info(
                "📋 queue: %d to process%s — starting",
                total,
                f", {failed} parked as failed" if failed else "",
            )
        progress = f"{batch_pos}/{total}"

        try:
            await _process(claimed, progress)
        except Exception:
            logger.exception("queue consumer %d hit an unexpected error", name)


async def run_worker() -> None:
    """Run CONCURRENCY consumer loops until shutdown. Started in lifespan."""
    global _shutdown
    _shutdown = False
    logger.info("queue worker starting (concurrency=%d)", CONCURRENCY)
    await asyncio.gather(*(_consumer(i) for i in range(CONCURRENCY)))
    logger.info("queue worker stopped")
