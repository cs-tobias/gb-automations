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
from dataclasses import dataclass

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
from gb_automations.sync.sync_frame import sync_frame_project, sync_frame_task
from gb_automations.sync.sync_labels import sync_project_labels
from gb_automations.sync.sync_tasks import sync_task_folder
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


@dataclass
class _Claimed:
    """A claimed task, flattened off the ORM object before its session closes."""

    id: int
    task_type: str
    user_email: str
    gmail_thread_id: str
    project_page_id: str | None
    attempts: int
    rebuild: bool


async def _claim() -> _Claimed | None:
    """Atomically claim the next task (any type), flattened for use after commit."""
    async with SessionLocal() as session:
        task = await claim_one(session)
        if task is None:
            return None
        claimed = _Claimed(
            id=task.id,
            task_type=task.task_type,
            user_email=task.user_email,
            gmail_thread_id=task.gmail_thread_id,
            project_page_id=task.project_page_id,
            attempts=task.attempts,
            rebuild=task.rebuild,
        )
        await session.commit()
        return claimed


async def _record_outcome(
    task_id: int,
    attempts: int,
    outcome_error: str | None,
    *,
    progress: str,
    label: str,
) -> bool:
    """Mark a claimed task done or failed (with retry/backoff). Returns True if it
    succeeded. `label` is a short human descriptor for logs (thread id or page id).

    Shared by the thread and label_sync paths so retry semantics stay identical
    across task types: below MAX_ATTEMPTS a failure backs off and retries; at the
    cap it parks as `failed` (visible in /debug/queue).
    """
    async with SessionLocal() as session:
        if outcome_error is None:
            await mark_done(session, task_id)
            await session.commit()
            logger.info("✔ task %s done — %s", progress, label)
            return True

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
                "✖ task %s FAILED for good after %d attempts (%s): %s",
                progress,
                attempts,
                label,
                outcome_error,
            )
        else:
            logger.warning(
                "↻ task %s failed (attempt %d/%d) — will retry: %s",
                progress,
                attempts,
                MAX_ATTEMPTS,
                outcome_error,
            )
        return False


async def _process_label_sync(claimed: _Claimed, progress: str) -> None:
    """Run a label_sync task: reconcile + create the project's Gmail label across
    all mailboxes (+ NAS folder), then light the project dot. No thread, so the
    per-thread Sync Queue mirror is skipped — the Projects-DB dot is the surface.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing labels for project %s%s", progress, project_page_id, retry_note
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError("label_sync task has no project_page_id")
        result = await sync_project_labels(project_page_id)
        # A mailbox that errored is a (likely transient) failure worth retrying;
        # surface it so _record_outcome backs off rather than marking done.
        if result.failed:
            outcome_error = f"label sync failed in mailbox(es): {', '.join(result.failed)}"
    except Exception as err:
        log_api_error(logger, f"label sync crashed for project {project_page_id}", err)
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id, claimed.attempts, outcome_error, progress=progress, label=str(project_page_id)
    )
    await queue_mirror.refresh_project_dot(project_page_id, progress=progress)


async def _process_task_folder_sync(claimed: _Claimed, progress: str) -> None:
    """Run a task_folder_sync task: provision NAS folders for one Oppgaver page.

    The task page id was stashed in gmail_thread_id at enqueue (no real thread).
    Project resolution happens INSIDE sync_task_folder (off the task's relation),
    so the queue row's project_page_id is null until the handler discovers it —
    we refresh the project dot at the end based on what the handler reports.
    """
    task_page_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing task folders for task %s%s", progress, task_page_id, retry_note
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    try:
        if not task_page_id:
            raise ValueError("task_folder_sync task has no task page id (gmail_thread_id)")
        result = await sync_task_folder(task_page_id)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "task folder sync failed"
    except Exception as err:
        log_api_error(logger, f"task folder sync crashed for task {task_page_id}", err)
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id, claimed.attempts, outcome_error, progress=progress, label=str(task_page_id)
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(project_page_id, progress=progress)


async def _process_frame_project_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_project_sync task: mirror the Notion Project to a folder
    under the shared Frame root project. Lights the Projects-DB dot via the
    same queue_mirror path label_sync uses — a Frame failure flips the same icon.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame project folder for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError("frame_project_sync task has no project_page_id")
        result = await sync_frame_project(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "frame project sync failed"
    except Exception as err:
        log_api_error(
            logger, f"frame project sync crashed for {project_page_id}", err
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(project_page_id, progress=progress)


async def _process_frame_task_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_task_sync task: mirror the Notion Task to a folder +
    placeholder file under its project's discipline subfolder in Frame.

    Like _process_task_folder_sync, the queue row's project_page_id starts NULL;
    sync_frame_task resolves it from the task page and we persist it onto the
    queue row so the Projects-DB rollup picks it up.
    """
    task_page_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame task folder for %s%s",
        progress,
        task_page_id,
        retry_note,
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    try:
        if not task_page_id:
            raise ValueError(
                "frame_task_sync task has no task page id (gmail_thread_id)"
            )
        result = await sync_frame_task(task_page_id)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "frame task sync failed"
    except Exception as err:
        log_api_error(logger, f"frame task sync crashed for {task_page_id}", err)
        outcome_error = describe_error(err)

    if project_page_id:
        # Persist the resolved project on the queue row so the Projects-DB dot
        # rollup finds this task even if a transient failure parks it as failed.
        await set_task_project(claimed.id, project_page_id)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(task_page_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(project_page_id, progress=progress)


async def _process(claimed: _Claimed, progress: str) -> None:
    """Dispatch a claimed task by type and record the outcome.

    `progress` is a human "N/M" batch position used purely for log narration.
    The claim already committed `in_progress` (so boot recovery sees a mid-run
    crash); the run and its outcome happen here in separate transactions.
    """
    if claimed.task_type == "label_sync":
        await _process_label_sync(claimed, progress)
        return
    if claimed.task_type == "task_folder_sync":
        await _process_task_folder_sync(claimed, progress)
        return
    if claimed.task_type == "frame_project_sync":
        await _process_frame_project_sync(claimed, progress)
        return
    if claimed.task_type == "frame_task_sync":
        await _process_frame_task_sync(claimed, progress)
        return

    task_id, email, thread_id, attempts, rebuild = (
        claimed.id,
        claimed.user_email,
        claimed.gmail_thread_id,
        claimed.attempts,
        claimed.rebuild,
    )

    retry_note = f" (retry {attempts}/{MAX_ATTEMPTS})" if attempts > 1 else ""
    verb = "rebuilding" if rebuild else "syncing"
    logger.info("▶ task %s — %s thread %s%s", progress, verb, thread_id, retry_note)

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
            # Live update: "we're on N/M, working on subject X". The worker's
            # post-completion call below will overwrite this once the task is
            # done. `progress` is the same N/M string the worker logs.
            await queue_mirror.refresh_project_dot(
                project_page_id, progress=progress, subject=subject
            )

    try:
        if rebuild:
            # Lazy import: resync_project imports sync_thread; importing it at
            # module load would risk a cycle with the worker's other imports.
            from gb_automations.sync.resync_project import rebuild_thread

            result = await rebuild_thread(thread_id, email, on_resolved=_on_resolved)
        else:
            result = await sync_thread(email, thread_id, on_resolved=_on_resolved)
        # result.errors entries can stringify to "" (e.g. an httpx ReadTimeout),
        # which would make the failure reason invisible — describe_error keeps
        # every entry meaningful (falls back to the exception class name).
        outcome_error = "; ".join(e for e in result.errors if e) or None if result.errors else None
    except Exception as err:
        log_api_error(logger, f"sync_thread crashed for {thread_id}", err)
        outcome_error = describe_error(err)
        result = None

    # Subject is computed up front so it's in scope for the post-completion
    # refresh_project_dot below on both success and failure paths.
    subject = (getattr(result, "thread_subject", None) or "").strip()

    async with SessionLocal() as session:
        if outcome_error is None:
            await mark_done(session, task_id)
            await session.commit()
            await queue_mirror.remove(thread_id)
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

    # Recompute the project dot now this task settled: 🔴 failed > 🟠 retrying
    # (a task failed once but is still retrying) > 🟢 active (siblings running) >
    # ⚪ idle. Prefer the project resolved this run; fall back to the persisted one.
    # `progress` is the worker's authoritative N/M for this batch — passed
    # through verbatim to the Notion "Sync progress" field along with the
    # subject so the field reads e.g. "5/17 - KG9 korreksjoner".
    project = resolved_project or getattr(result, "project_page_id", None)
    await queue_mirror.refresh_project_dot(project, progress=progress, subject=subject)


class _TaskStub:
    """Minimal stand-in for a SyncTask row, carrying the fields mark_failed reads."""

    __slots__ = ("id", "attempts")

    def __init__(self, id: int, attempts: int) -> None:  # noqa: A002 - matches column name
        self.id = id
        self.attempts = attempts


async def _pending_now() -> int:
    """How many tasks are still waiting (claimable now or after backoff)."""
    return (await status_counts()).get("pending", 0)


def _rss_mb() -> float:
    """Current process RSS in MiB, read from /proc/self/status. Lightweight
    diagnostic for tracking whether memory grows linearly across a batch
    (= leak somewhere) or stays bounded (= host pressure is the issue).
    Returns 0.0 if /proc isn't readable (e.g. macOS dev), so logging stays safe.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024.0  # kB → MiB
    except OSError:
        pass
    return 0.0


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
                "📋 queue: %d to process%s — starting (RSS %.0fMiB)",
                total,
                f", {failed} parked as failed" if failed else "",
                _rss_mb(),
            )
        progress = f"{batch_pos}/{total}"
        # Per-task RSS log: lets us see (in the next prod resync's logs) whether
        # memory climbs linearly across the batch (= leak) or stays bounded
        # (= the production PC just doesn't have enough headroom — host issue,
        # not code). Cheap: a single /proc read per task, microseconds.
        logger.info("   mem rss=%.0fMiB before task %s", _rss_mb(), progress)

        try:
            await _process(claimed, progress)
        except Exception:
            logger.exception("queue consumer %d hit an unexpected error", name)
        logger.info("   mem rss=%.0fMiB after task %s", _rss_mb(), progress)


async def run_worker() -> None:
    """Run CONCURRENCY consumer loops until shutdown. Started in lifespan."""
    global _shutdown
    _shutdown = False
    logger.info("queue worker starting (concurrency=%d)", CONCURRENCY)
    await asyncio.gather(*(_consumer(i) for i in range(CONCURRENCY)))
    logger.info("queue worker stopped")
