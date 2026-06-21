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
from gb_automations.models import FrameLeveranseFolder
from gb_automations.obs import describe_error, log_api_error
from gb_automations.sync import queue_mirror
from gb_automations.sync.queue import (
    claim_one,
    mark_done,
    mark_failed,
    set_task_project,
    status_counts,
)
from gb_automations.sync.dispatch_project_status import dispatch_project_status
from gb_automations.sync.sync_fiken_invoice import create_fiken_invoice
from gb_automations.sync.sync_frame import sync_frame_leveranse, sync_frame_project
from gb_automations.sync.sync_frame_comments import sync_frame_comment
from gb_automations.sync.sync_frame_file_status import sync_frame_file_status
from gb_automations.sync.sync_frame_project_status import sync_frame_project_status
from gb_automations.sync.sync_frame_version import sync_frame_version
from gb_automations.sync.sync_labels import sync_project_labels
from gb_automations.sync.sync_leveranse_status import recheck_leveranse_status
from gb_automations.sync.sync_nas import sync_nas_folder
from gb_automations.sync.sync_oppgave_done import sync_oppgave_done
from gb_automations.sync.sync_tasks import sync_task_folder
from gb_automations.sync.sync_thread import sync_thread
from gb_automations.sync.sync_toggl_hours import sync_toggl_hours
from gb_automations.sync.sync_toggl_project import sync_toggl_project

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


async def _resolve_project_from_leveranse(
    leveranse_page_id: str | None,
) -> str | None:
    """Look up the Frame leveranse → project mapping in Postgres.

    Used by the Frame engines whose result dataclasses don't carry
    `project_page_id` (`oppgave_done_sync`, `leveranse_status_recheck`)
    so they can still light the Projects-DB dot + write the "Sync
    progress" subject. Returns None silently on cache miss / DB error —
    `refresh_project_dot(None, ...)` is already a safe no-op, so a miss
    just means the field doesn't get updated for this task. The mapping
    is owned by `sync_frame_leveranse`, so a cache miss only happens if
    a Frame engine fired on a Notion page that wasn't provisioned by
    this codebase (legitimate edge — we just stay quiet).
    """
    if not leveranse_page_id:
        return None
    try:
        async with SessionLocal() as session:
            row = await session.get(FrameLeveranseFolder, leveranse_page_id)
    except Exception:  # noqa: BLE001
        return None
    if row is None:
        return None
    return row.project_page_id


async def _dot_in_progress(
    project_page_id: str | None, *, progress: str, subject: str
) -> None:
    """Write the project's "Sync progress" subject WHILE the task is still
    in_progress.

    Why this and not the post-completion `refresh_project_dot`: the dot
    writer reads `project_sync_state` straight from sync_tasks. By the
    time `_record_outcome` flips the row to `done`, a single-task batch
    has nothing else in flight for the project — state goes `idle` and
    the writer's idle-clear branch overrides whatever `progress=` we
    pass, blanking the field instantly. Calling here, BEFORE
    `_record_outcome`, the row is still `in_progress` → state is
    `active` → the subject sticks until the next task overwrites it
    (or the post-completion call clears it once the queue is truly
    drained).

    Best-effort: `refresh_project_dot` already swallows its own
    exceptions, so callers don't need a try/except.
    """
    if not project_page_id:
        return
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject=subject
    )


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

    # Mid-task progress write: state is still 'active' (the task is in_progress),
    # so the "N/M - Gmail labels" string lands in the Notion field. The
    # post-completion refresh_project_dot at the bottom of this function will
    # then clear it once the queue goes idle, so a fast per-system click flashes
    # briefly visible and disappears once done — the visibility the operator
    # wanted on single-button clicks.
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Gmail labels"
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
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Gmail labels"
    )


async def _process_nas_folder_sync(claimed: _Claimed, progress: str) -> None:
    """Run a nas_folder_sync task: provision the project's folder structure on
    the office NAS share, write the Windows-facing path back to the Projects-DB
    "NAS" URL column, and light the project dot via the same queue_mirror path
    label_sync uses.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing NAS folder for project %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="NAS folder"
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError("nas_folder_sync task has no project_page_id")
        result = await sync_nas_folder(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "nas folder sync failed"
    except Exception as err:
        log_api_error(logger, f"nas folder sync crashed for {project_page_id}", err)
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="NAS folder"
    )


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

    # Mid-task subject write — `_record_outcome` below flips the task to
    # done, after which a single-task batch reads as idle and the post-
    # completion call clears the field. We want the subject visible
    # while the task is still in flight.
    await _dot_in_progress(project_page_id, progress=progress, subject="Task folder")

    await _record_outcome(
        claimed.id, claimed.attempts, outcome_error, progress=progress, label=str(task_page_id)
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Task folder"
        )


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

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Frame.io folder"
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
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Frame.io folder"
    )


async def _process_frame_project_status_sync(
    claimed: _Claimed, progress: str
) -> None:
    """Run a frame_project_status_sync task: propagate the Notion Project's
    current `Status` to the linked Frame project's V4 `status` field
    (active ↔ inactive). Lights the same Projects-DB dot label_sync uses
    so a Frame failure surfaces in the same place.

    Separate from frame_project_sync: that task provisions the folder
    tree + V00 placeholders, this one only flips the lifecycle flag. A
    project whose Frame entity isn't yet provisioned (no
    FrameProjectFolder row) is a clean skip — the engine no-ops.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame project active/inactive for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Frame.io status"
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError(
                "frame_project_status_sync task has no project_page_id"
            )
        result = await sync_frame_project_status(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "frame project status sync failed"
    except Exception as err:
        log_api_error(
            logger,
            f"frame project status sync crashed for {project_page_id}",
            err,
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Frame.io status"
    )


async def _process_project_status_dispatch(
    claimed: _Claimed, progress: str
) -> None:
    """Run a project_status_dispatch task: the worker-side dispatcher for a
    Notion Projects-DB `Status` change.

    The /webhooks/notion/project-status endpoint only acks Notion (validates
    bearer + enqueues this one row + returns 200) so Notion's auto-pause
    heuristic doesn't fire on a slow inline response. All the actual work —
    Notion get_page, placeholder-title gate, status read, fan-out to every
    per-engine task type (label_sync / nas_folder_sync / toggl_project_sync /
    frame_project_sync + per-leveranse Frame fan-out / frame_project_status_sync)
    — happens here. Idempotent: every downstream enqueue is dedup'd by its
    own active-task unique index, so a re-fired dispatch is safe.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — dispatching Notion project-status change for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Status dispatch"
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError(
                "project_status_dispatch task has no project_page_id"
            )
        result = await dispatch_project_status(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "project status dispatch failed"
    except Exception as err:
        log_api_error(
            logger,
            f"project status dispatch crashed for {project_page_id}",
            err,
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Status dispatch"
    )


async def _process_graduate_faktura(
    claimed: _Claimed, progress: str
) -> None:
    """Run a graduate_faktura task: scan Fiken for sent invoices that
    belong to this project, write Faktura DB rows, graduate statuses
    + Fakturert beløp, clear the draft URL.

    Engine: `sync/graduate_project.py::graduate_project`. Same engine
    the future hourly poller (C.3b) will call in a per-project loop.
    """
    from gb_automations.sync.graduate_project import graduate_project

    project_page_id = claimed.project_page_id or claimed.gmail_thread_id
    retry_note = (
        f" (retry {claimed.attempts}/{MAX_ATTEMPTS})"
        if claimed.attempts > 1
        else ""
    )
    logger.info(
        "▶ task %s — graduate_faktura for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id,
        progress=progress,
        subject="Sjekk fiken",
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError(
                "graduate_faktura task has no project_page_id"
            )
        summary = await graduate_project(project_page_id)
        if summary.error:
            outcome_error = summary.error
        else:
            logger.info(
                "✓ graduate_faktura for %s: scanned=%d matched=%d "
                "skipped_already=%d skipped_no_match=%d",
                project_page_id,
                summary.fiken_invoices_scanned,
                len(summary.matched),
                summary.skipped_already_graduated,
                summary.skipped_no_match,
            )
    except Exception as err:
        log_api_error(
            logger,
            f"graduate_faktura crashed for {project_page_id}",
            err,
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id,
        progress=progress,
        subject="Sjekk fiken",
    )


async def _process_send_faktura(
    claimed: _Claimed, progress: str
) -> None:
    """Run a send_faktura task: create a DRAFT invoice in Fiken for a
    Notion Project. The engine reads the project's `Faktura status` to
    decide oppstart vs slutt; the task itself carries only the
    project_page_id.

    Engine: `sync/sync_fiken_invoice.py::create_fiken_invoice`.
    """
    project_page_id = claimed.project_page_id or claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — send_faktura draft for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Send faktura draft"
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError("send_faktura task has no project_page_id")
        result = await create_fiken_invoice(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "send_faktura: invoice creation failed"
    except Exception as err:
        log_api_error(
            logger,
            f"send_faktura crashed for {project_page_id}",
            err,
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Send faktura draft"
    )


async def _process_toggl_hours_sync(claimed: _Claimed, progress: str) -> None:
    """Run a toggl_hours_sync task: nightly aggregation from Toggl Reports
    v3 → year-partitioned `Timer YYYY` Notion DB.

    Unlike the project-scoped task types, this one isn't tied to a single
    Notion page — it's a workspace-wide aggregator. So no project dot
    update, no per-row queue mirror; the engine's summary log line is
    the observability surface.
    """
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — running Toggl hours daily aggregator%s",
        progress,
        retry_note,
    )

    outcome_error: str | None = None
    try:
        result = await sync_toggl_hours()
        if result.action == "failed":
            outcome_error = result.note or "toggl hours sync failed"
    except Exception as err:
        log_api_error(logger, "toggl hours sync crashed", err)
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label="toggl-hours",
    )


async def _process_toggl_project_sync(claimed: _Claimed, progress: str) -> None:
    """Run a toggl_project_sync task: mirror the Notion Project to a Toggl
    Track project under the configured workspace. Lights the Projects-DB
    dot via the same queue_mirror path label_sync uses — a Toggl failure
    flips the same icon.

    Phase 1 of the Toggl integration. Project mirror only; the daily hours
    aggregation lands as a separate task type in a later slice.
    """
    project_page_id = claimed.project_page_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Toggl project for %s%s",
        progress,
        project_page_id,
        retry_note,
    )

    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Toggl project"
    )

    outcome_error: str | None = None
    try:
        if not project_page_id:
            raise ValueError("toggl_project_sync task has no project_page_id")
        result = await sync_toggl_project(project_page_id)
        if result.action == "failed":
            outcome_error = result.note or "toggl project sync failed"
    except Exception as err:
        log_api_error(
            logger, f"toggl project sync crashed for {project_page_id}", err
        )
        outcome_error = describe_error(err)

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(project_page_id),
    )
    await queue_mirror.refresh_project_dot(
        project_page_id, progress=progress, subject="Toggl project"
    )


async def _process_frame_leveranse_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_leveranse_sync task: mirror the Notion Leveranse to a
    folder + placeholder file under its project's discipline subfolder in
    Frame.

    Like _process_task_folder_sync, the queue row's project_page_id starts
    NULL; sync_frame_leveranse resolves it from the leveranse page and we
    persist it onto the queue row so the Projects-DB rollup picks it up.
    """
    leveranse_page_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame leveranse folder for %s%s",
        progress,
        leveranse_page_id,
        retry_note,
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    result = None
    try:
        if not leveranse_page_id:
            raise ValueError(
                "frame_leveranse_sync task has no leveranse page id (gmail_thread_id)"
            )
        result = await sync_frame_leveranse(leveranse_page_id)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "frame leveranse sync failed"
    except Exception as err:
        log_api_error(logger, f"frame leveranse sync crashed for {leveranse_page_id}", err)
        outcome_error = describe_error(err)

    if project_page_id:
        # Persist the resolved project on the queue row so the Projects-DB dot
        # rollup finds this task even if a transient failure parks it as failed.
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Frame.io deliverable"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(leveranse_page_id),
    )
    # Visibility for the self-heal chain: if the leveranse engine
    # discovered a pre-existing version stack on top of V00 and
    # enqueued an audit, surface that on a separate log line so the
    # operator can follow the chain (leveranse done → audit queued →
    # audit runs → status recheck).
    if result is not None and result.audit_queued and result.audit_stack_id:
        logger.info(
            "↳ audit queued for stack %s (leveranse %s)",
            result.audit_stack_id,
            leveranse_page_id,
        )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Frame.io deliverable"
        )


async def _process_frame_comment_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_comment_sync task: write one Frame.io comment into its
    Korreksjonsrunde Oppgave's page body as a Notion bullet.

    Same shape as _process_frame_leveranse_sync — the queue row carries
    the Frame comment UUID in gmail_thread_id (placeholder slot), and
    project_page_id is resolved from the engine result so the Projects-DB
    dot picks up comment activity too. Many engine outcomes (V00 noise,
    untracked-file comment, deleted-comment 404) are 'skipped' — those
    are healthy no-ops and mark the task done, NOT failed.
    """
    comment_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame comment %s%s",
        progress,
        comment_id,
        retry_note,
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    try:
        if not comment_id:
            raise ValueError(
                "frame_comment_sync task has no comment id (gmail_thread_id)"
            )
        result = await sync_frame_comment(comment_id)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "frame comment sync failed"
    except Exception as err:
        log_api_error(logger, f"frame comment sync crashed for {comment_id}", err)
        outcome_error = describe_error(err)

    if project_page_id:
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Frame comment"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(comment_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Frame comment"
        )


async def _process_frame_version_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_version_sync task: a new version was uploaded on a
    tracked Leveranse's version stack → flip Leveranse status to Ferdig.

    The queue row carries the Frame version_stack UUID in gmail_thread_id
    (placeholder slot). The engine resolves the Leveranse and writes the
    status; project_page_id is set so the Projects-DB dot picks it up too.
    "Skipped" outcomes (stack 4xx, untracked stack, only V00) are healthy
    no-ops, NOT failed.
    """
    stack_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — syncing Frame version stack %s%s",
        progress,
        stack_id,
        retry_note,
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    try:
        if not stack_id:
            raise ValueError(
                "frame_version_sync task has no stack id (gmail_thread_id)"
            )
        result = await sync_frame_version(stack_id)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "frame version sync failed"
    except Exception as err:
        log_api_error(logger, f"frame version sync crashed for {stack_id}", err)
        outcome_error = describe_error(err)

    if project_page_id:
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Frame version"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(stack_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Frame version"
        )


async def _process_frame_file_status_sync(claimed: _Claimed, progress: str) -> None:
    """Run a frame_file_status_sync task: reconcile Notion deliverable
    Status ↔ Frame.io custom Status field for one deliverable (Utgår
    mirror).

    `gmail_thread_id` carries the Notion deliverable page id;
    `user_email` carries the source hint ("notion" or "frame") that
    tells the engine which side is authoritative for THIS pass.
    "Skipped"/"unchanged" outcomes are healthy (loop-guard caught an
    echo, or sync disabled by env), NOT failed.
    """
    leveranse_page_id = claimed.gmail_thread_id
    source = claimed.user_email or "notion"
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — reconciling Frame file Status for %s (source=%s)%s",
        progress,
        leveranse_page_id,
        source,
        retry_note,
    )

    outcome_error: str | None = None
    project_page_id: str | None = None
    try:
        if not leveranse_page_id:
            raise ValueError(
                "frame_file_status_sync task has no leveranse page id (gmail_thread_id)"
            )
        result = await sync_frame_file_status(leveranse_page_id, source=source)
        project_page_id = result.project_page_id
        if result.action == "failed":
            outcome_error = result.note or "frame file status sync failed"
    except Exception as err:
        log_api_error(
            logger, f"frame file status sync crashed for {leveranse_page_id}", err
        )
        outcome_error = describe_error(err)

    if project_page_id:
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Frame status"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(leveranse_page_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Frame status"
        )


async def _process_oppgave_done_sync(claimed: _Claimed, progress: str) -> None:
    """Run an oppgave_done_sync task: propagate a Notion Korreksjon's
    Ferdig checkbox state to its linked Frame comment.

    Most loop-iteration outcomes are 'skipped_loop' (Frame already
    matches Notion — we're seeing our own write bouncing back). Those
    are healthy no-ops. The engine result carries `leveranse_page_id`,
    so we can walk one step further (FrameLeveranseFolder → project)
    to light the Projects-DB dot + write the "Sync progress" subject.
    """
    oppgave_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — propagating Oppgave done state for %s%s",
        progress,
        oppgave_id,
        retry_note,
    )

    outcome_error: str | None = None
    leveranse_page_id: str | None = None
    try:
        if not oppgave_id:
            raise ValueError(
                "oppgave_done_sync task has no Oppgave page id (gmail_thread_id)"
            )
        result = await sync_oppgave_done(oppgave_id)
        leveranse_page_id = result.leveranse_page_id
        if result.action == "failed":
            outcome_error = result.note or "oppgave done sync failed"
    except Exception as err:
        log_api_error(logger, f"oppgave done sync crashed for {oppgave_id}", err)
        outcome_error = describe_error(err)

    project_page_id = await _resolve_project_from_leveranse(leveranse_page_id)
    if project_page_id:
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Oppgave done"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(oppgave_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Oppgave done"
        )


async def _process_leveranse_status_recheck(claimed: _Claimed, progress: str) -> None:
    """Run a leveranse_status_recheck task: recompute the Leveranse's
    status from the active Korreksjonsrunde's Korreksjon children.

    The engine is pure read-then-decide-then-write logic; failure modes
    are Notion API errors only.
    """
    leveranse_id = claimed.gmail_thread_id
    retry_note = f" (retry {claimed.attempts}/{MAX_ATTEMPTS})" if claimed.attempts > 1 else ""
    logger.info(
        "▶ task %s — rechecking Leveranse status for %s%s",
        progress,
        leveranse_id,
        retry_note,
    )

    outcome_error: str | None = None
    try:
        if not leveranse_id:
            raise ValueError(
                "leveranse_status_recheck task has no Leveranse page id (gmail_thread_id)"
            )
        result = await recheck_leveranse_status(leveranse_id)
        if result.action == "failed":
            outcome_error = result.note or "leveranse status recheck failed"
    except Exception as err:
        log_api_error(
            logger, f"leveranse status recheck crashed for {leveranse_id}", err
        )
        outcome_error = describe_error(err)

    # The task's `gmail_thread_id` IS the leveranse page id, so we go
    # straight to the FrameLeveranseFolder lookup to light the dot.
    project_page_id = await _resolve_project_from_leveranse(leveranse_id)
    if project_page_id:
        await set_task_project(claimed.id, project_page_id)

    await _dot_in_progress(
        project_page_id, progress=progress, subject="Status recheck"
    )

    await _record_outcome(
        claimed.id,
        claimed.attempts,
        outcome_error,
        progress=progress,
        label=str(leveranse_id),
    )
    if project_page_id:
        await queue_mirror.refresh_project_dot(
            project_page_id, progress=progress, subject="Status recheck"
        )


async def _process(claimed: _Claimed, progress: str) -> None:
    """Dispatch a claimed task by type and record the outcome.

    `progress` is a human "N/M" batch position used purely for log narration.
    The claim already committed `in_progress` (so boot recovery sees a mid-run
    crash); the run and its outcome happen here in separate transactions.
    """
    if claimed.task_type == "label_sync":
        await _process_label_sync(claimed, progress)
        return
    if claimed.task_type == "nas_folder_sync":
        await _process_nas_folder_sync(claimed, progress)
        return
    if claimed.task_type == "task_folder_sync":
        await _process_task_folder_sync(claimed, progress)
        return
    if claimed.task_type == "frame_project_sync":
        await _process_frame_project_sync(claimed, progress)
        return
    if claimed.task_type == "frame_project_status_sync":
        await _process_frame_project_status_sync(claimed, progress)
        return
    if claimed.task_type == "project_status_dispatch":
        await _process_project_status_dispatch(claimed, progress)
        return
    if claimed.task_type == "send_faktura":
        await _process_send_faktura(claimed, progress)
        return
    if claimed.task_type == "graduate_faktura":
        await _process_graduate_faktura(claimed, progress)
        return
    if claimed.task_type == "toggl_project_sync":
        await _process_toggl_project_sync(claimed, progress)
        return
    if claimed.task_type == "toggl_hours_sync":
        await _process_toggl_hours_sync(claimed, progress)
        return
    if claimed.task_type == "frame_leveranse_sync":
        await _process_frame_leveranse_sync(claimed, progress)
        return
    if claimed.task_type == "frame_comment_sync":
        await _process_frame_comment_sync(claimed, progress)
        return
    if claimed.task_type == "frame_version_sync":
        await _process_frame_version_sync(claimed, progress)
        return
    if claimed.task_type == "frame_file_status_sync":
        await _process_frame_file_status_sync(claimed, progress)
        return
    if claimed.task_type == "oppgave_done_sync":
        await _process_oppgave_done_sync(claimed, progress)
        return
    if claimed.task_type == "leveranse_status_recheck":
        await _process_leveranse_status_recheck(claimed, progress)
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
