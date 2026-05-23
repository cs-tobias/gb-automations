"""Best-effort live mirror of the Postgres sync queue into a Notion DB.

The client watches a Notion "Sync Queue" database to see what's queued,
processing, or failed. This module is the thin, FAILURE-ISOLATED bridge from
the worker/webhook to that Notion view: every call swallows its own exceptions
and logs them, because the Notion mirror is a convenience — it must never block
or fail a sync. The Postgres `sync_tasks` table remains the source of truth
(observable via /debug/queue) regardless of whether these writes land.

No-op throughout when `SYNC_QUEUE_DB_ID` is unset (dev / not yet configured).
"""

from __future__ import annotations

import logging

from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    PROJECT_SYNC_OPTION,
    SYNC_QUEUE_STATUS_FAILED,
    SYNC_QUEUE_STATUS_PROCESSING,
    SYNC_QUEUE_STATUS_QUEUED,
    settings,
)

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(settings.sync_queue_db_id)


def _dot_enabled() -> bool:
    return bool(settings.projects_sync_status)


async def mark_queued(thread_id: str, subject: str, project: str | None = None) -> None:
    if not _enabled():
        return
    try:
        await notion_client.upsert_sync_queue_row(
            thread_id=thread_id,
            subject=subject,
            status=SYNC_QUEUE_STATUS_QUEUED,
            project=project,
        )
    except Exception:
        logger.warning("sync-queue mirror: mark_queued failed for %s", thread_id, exc_info=True)


async def mark_processing(thread_id: str, subject: str, project: str | None = None) -> None:
    if not _enabled():
        return
    try:
        await notion_client.upsert_sync_queue_row(
            thread_id=thread_id,
            subject=subject,
            status=SYNC_QUEUE_STATUS_PROCESSING,
            project=project,
        )
    except Exception:
        logger.warning("sync-queue mirror: mark_processing failed for %s", thread_id, exc_info=True)


async def mark_failed(thread_id: str, subject: str, error: str, *, result=None) -> None:
    if not _enabled():
        return
    project = getattr(result, "project_name", None) if result is not None else None
    subject = (getattr(result, "thread_subject", None) if result is not None else None) or subject
    try:
        await notion_client.upsert_sync_queue_row(
            thread_id=thread_id,
            subject=subject,
            status=SYNC_QUEUE_STATUS_FAILED,
            project=project,
            error=error,
        )
    except Exception:
        logger.warning("sync-queue mirror: mark_failed failed for %s", thread_id, exc_info=True)


async def remove(thread_id: str) -> None:
    """Drop the worklist row once a thread is done (the email lives in Emails DB)."""
    if not _enabled():
        return
    try:
        await notion_client.remove_sync_queue_row(thread_id)
    except Exception:
        logger.warning("sync-queue mirror: remove failed for %s", thread_id, exc_info=True)


async def refresh_project_dot(
    project_page_id: str | None,
    *,
    progress: str | None = None,
) -> None:
    """Recompute and write a project's sync icon + live "<done>/<total>" counter.

    Icon: derived from sync_tasks via project_sync_state — active > failed >
    retrying > idle. Correct even when several threads for the same project
    overlap (only goes ✅ once NO task for the project is in flight).

    Progress: the worker has the authoritative "N/M" in memory (it logs it on
    every task — "▶ task 5/17 …"), so we just pass it through. The progress
    string IS the worker's `batch_pos/total` for the consumer that finished
    this task — the same number the user sees in the log. Trying to recompute
    it from the DB was unreliable because many task rows don't have
    project_page_id set until the worker processes them (it's resolved lazily
    inside sync_thread). Passing `progress=None` (e.g. the enqueue webhook
    path) only updates the icon and leaves the existing progress text alone;
    when the worker fires `refresh_project_dot(..., progress="3/17")` it
    overwrites it. When the project goes idle the progress is cleared.

    Best-effort throughout: independent try/except per write so one failure
    can't suppress the other, and either failing is logged but never raised
    (the Notion mirror is a convenience; sync_tasks remains the source of
    truth, observable via /debug/queue).
    """
    if not _dot_enabled() or not project_page_id:
        return
    # Imported here (not at module top) to avoid a queue ↔ mirror import cycle.
    from gb_automations.sync.queue import project_sync_state

    state: str | None = None
    try:
        state = await project_sync_state(project_page_id)
        await notion_client.set_project_sync_status(
            project_page_id, PROJECT_SYNC_OPTION[state]
        )
    except Exception:
        logger.warning(
            "project-dot: status refresh failed for %s", project_page_id, exc_info=True
        )

    # Progress write rules:
    #   - state idle → clear (project is fully done, no batch in flight).
    #   - progress given → write it verbatim (worker just finished a task).
    #   - progress None and not idle → leave the existing text alone (we
    #     haven't been told a new number, e.g. this is the enqueue path).
    try:
        if state == "idle":
            await notion_client.set_project_sync_progress(project_page_id, None)
        elif progress is not None:
            await notion_client.set_project_sync_progress(project_page_id, progress)
    except Exception:
        logger.warning(
            "project-dot: progress refresh failed for %s", project_page_id, exc_info=True
        )
