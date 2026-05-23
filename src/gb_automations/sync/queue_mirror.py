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
from typing import Any

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
    subject: str | None = None,
) -> None:
    """Recompute and write a project's sync icon + live "<done>/<total> - subject" counter.

    Icon: derived from sync_tasks via project_sync_state — active > failed >
    retrying > idle. Correct even when several threads for the same project
    overlap (only goes ✅ once NO task for the project is in flight).

    Progress + subject: the worker has the authoritative "N/M" + subject in
    memory (it logs both on every task — "▶ task 5/17 — rebuilding thread X"
    / "✔ task 5/17 done — 'KG9 - korrektur'"), so we just pass them through.
    When both are given, the Notion field reads "N/M - subject"; without a
    subject it's just "N/M". Trying to recompute progress from the DB was
    unreliable because many task rows don't have project_page_id set until the
    worker processes them (resolved lazily inside sync_thread).

    `progress=None` (e.g. the enqueue webhook path) leaves the existing field
    alone unless the project went idle (then we clear). The worker calls this
    twice per thread task: once mid-sync from `_on_resolved` (live "we're on
    this one now") and once post-completion (final "N/M done with subject").
    Once the whole batch drains and state == idle, the field is cleared so an
    at-rest project shows no leftover progress text — only the ✅ icon.

    Best-effort: any failure is logged but never raised (the Notion mirror is
    a convenience; sync_tasks remains the source of truth via /debug/queue).
    """
    if not _dot_enabled() or not project_page_id:
        return
    # Imported here (not at module top) to avoid a queue ↔ mirror import cycle.
    from gb_automations.sync.queue import project_sync_state

    try:
        state = await project_sync_state(project_page_id)
    except Exception:
        # If we can't compute state we can't update Notion safely — bail.
        logger.warning(
            "project-dot: state lookup failed for %s", project_page_id, exc_info=True
        )
        return

    # Progress write rules (priority order, must match this priority):
    #   1. state idle → clear. Idle means the queue has drained (no pending,
    #      in_progress, or failed tasks for this project), so any in-flight
    #      progress text is now stale. This intentionally OVERRIDES even a
    #      `progress` arg from the worker's post-completion call: when the
    #      final task of an N-task batch completes, the state is already idle
    #      and we want a clean field, not a sticky "17/17 - …" leftover.
    #   2. `progress` given (and not idle) → write it verbatim, with
    #      " - subject" appended when one is supplied.
    #   3. else (not idle, no progress) → DON'T touch the progress field
    #      (mid-sync `_on_resolved` callback; the post-completion call will
    #      fill in the right number). The combined writer's _PROGRESS_UNCHANGED
    #      sentinel handles this by simply omitting the property from the PATCH.
    if state == "idle":
        progress_arg: Any = None
        progress_desc = "<clear>"
    elif progress is not None:
        # Stitch in the subject when given. Trimming is defensive — Notion
        # rich_text accepts up to ~2000 chars but anything past ~80 makes the
        # field unreadable in the DB table view.
        if subject:
            clean_subject = subject.strip()
            if len(clean_subject) > 80:
                clean_subject = clean_subject[:77] + "…"
            progress_arg = f"{progress} - {clean_subject}"
        else:
            progress_arg = progress
        progress_desc = repr(progress_arg)
    else:
        # Sentinel — don't include progress in the PATCH at all.
        progress_arg = notion_client._PROGRESS_UNCHANGED
        progress_desc = "<unchanged>"

    logger.info(
        "project-dot: state=%s progress=%s for %s", state, progress_desc, project_page_id
    )
    try:
        await notion_client.set_project_sync_state(
            project_page_id,
            status_option=PROJECT_SYNC_OPTION[state],
            progress=progress_arg,
        )
    except Exception:
        # exc_info=True surfaces the underlying Notion error (e.g. property
        # missing / wrong type) so we can fix it without guessing.
        logger.warning(
            "project-dot: refresh failed for %s", project_page_id, exc_info=True
        )
