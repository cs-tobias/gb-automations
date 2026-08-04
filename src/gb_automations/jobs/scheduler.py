"""APScheduler setup — runs scheduled background jobs inside the FastAPI process.

Currently:
  - Weekly Gmail watch renewal. Watches expire after 7 days; we renew every 5 days
    to leave headroom in case a run is missed.
  - Daily Toggl hours aggregation (when SYNC_TOGGL_HOURS=true). Fires at
    02:00 Europe/Oslo so the previous day's entries are settled before the
    pull. Only ENQUEUES a task — the durable worker runs the actual sync,
    so a long Reports v3 call can't trip the scheduler's misfire timeout.
  - Daily Frame.io refresh-token keepalive. Adobe IMS rotates the token on
    every refresh call and the original expires after 14 days; a quiet week
    (holidays, low Frame activity) would otherwise let the clock run out.

The scheduler is started in main.py's lifespan handler and shut down on app exit.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gb_automations.config import settings
from gb_automations.jobs import queue_worker
from gb_automations.sync.queue import enqueue_toggl_hours_sync
from gb_automations.sync.watches import renew_all_watches

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


async def _enqueue_toggl_hours_job() -> None:
    """Scheduler-side wrapper: enqueue + wake worker.

    Kept separate from `enqueue_toggl_hours_sync` so the queue helper stays
    a pure DB op (no worker coupling) while the scheduler can fire-and-
    forget. A duplicate enqueue at 02:00 (e.g. if APScheduler ever fires
    twice on DST) collapses to one row via the singleton dedup index.
    """
    inserted = await enqueue_toggl_hours_sync()
    queue_worker.wake()
    if inserted:
        logger.info("⏰ toggl_hours_sync enqueued by scheduler")
    else:
        logger.info(
            "⏰ toggl_hours_sync skipped — one is already active in the queue"
        )


async def _enqueue_fiken_graduations_for_all_active_projects() -> None:
    """Hourly Fiken-graduation poller.

    Queries the Projects DB for every row whose `Faktura status` is in
    a non-terminal state (anything except blank / Ikke fakturert /
    Fakturert / Kreditert) and enqueues a `graduate_faktura` task per
    project. The worker dispatches each to the same `graduate_project`
    engine the operator's "Sjekk fiken" button uses.

    The queue's per-project dedup (uq_sync_tasks_active_graduate_
    faktura) collapses the cron-enqueue with any in-flight operator
    click, so racing is safe.

    Best-effort overall: any exception during the scan logs + returns
    (next hour retries). Per-project enqueue failures don't block the
    rest — they're logged inline.
    """
    from gb_automations.clients import notion as notion_client
    from gb_automations.config import (
        FAKTURA_STATUS_50,
        FAKTURA_STATUS_FULL,
        FAKTURA_STATUS_IKKE,
        FAKTURA_STATUS_KREDITERT,
        PROJECTS_FAKTURA_STATUS_PROP,
    )
    from gb_automations.sync.queue import enqueue_graduate_faktura

    if not settings.projects_db_id:
        logger.info(
            "⏰ fiken_graduations cron: PROJECTS_DB_ID not set; skipping."
        )
        return

    # "Active" = anything not at a terminal end-state. Blank counts as
    # terminal (operator hasn't queued anything). The actively-polled
    # (non-terminal) set is exactly the four OPERATOR-INTENT values:
    # Til oppstartsfaktura / Til avslutningsfaktura / Til fakturering /
    # Til kreditering.
    #
    # `Oppstart fakturert` (FAKTURA_STATUS_50) is terminal HERE even
    # though it's mid-lifecycle: it's an ENGINE-WRITTEN resting state
    # after the 50% invoice graduated (sent_at stamped), so re-scanning
    # can only ever produce matched=0 / skipped_already. The project
    # re-enters the poller the instant the operator flips it to
    # Til avslutningsfaktura (bill the rest). Leaving it out of this set
    # caused every 50%-billed project to be re-enqueued EVERY HOUR
    # forever, each re-scanning Fiken's full invoice catalogue for
    # nothing — see gotchas.md.
    terminal = {
        None,
        "",
        FAKTURA_STATUS_IKKE,
        FAKTURA_STATUS_50,
        FAKTURA_STATUS_FULL,
        FAKTURA_STATUS_KREDITERT,
    }

    try:
        rows = await notion_client.query_database(settings.projects_db_id)
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "⏰ fiken_graduations cron: Projects DB query failed (%s); "
            "skipping this hour.",
            err,
        )
        return

    enqueued = 0
    skipped_terminal = 0
    skipped_dedup = 0
    errors = 0
    for row in rows:
        status = notion_client.read_select_name(
            row, PROJECTS_FAKTURA_STATUS_PROP
        )
        if status in terminal:
            skipped_terminal += 1
            continue
        page_id = row.get("id") or ""
        if not page_id:
            continue
        try:
            inserted = await enqueue_graduate_faktura(page_id)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "⏰ fiken_graduations cron: enqueue failed for %s: %s",
                page_id,
                err,
            )
            errors += 1
            continue
        if inserted:
            enqueued += 1
        else:
            skipped_dedup += 1

    if enqueued:
        queue_worker.wake()
    logger.info(
        "⏰ fiken_graduations cron: scanned=%d enqueued=%d "
        "skipped_terminal=%d skipped_dedup=%d errors=%d",
        len(rows),
        enqueued,
        skipped_terminal,
        skipped_dedup,
        errors,
    )


async def _frame_token_keepalive() -> None:
    """Force a Frame refresh-token rotation so the 14-day clock can't run out.

    Adobe IMS rotates the refresh token on every refresh call. Even an idle
    deployment (no Frame webhook traffic for a week+) needs SOMEONE to call
    /token before day 14, or the original token expires and the integration
    breaks with access_denied. Failure here is logged but never raised —
    the scheduler must keep ticking other jobs even if Adobe is down.
    """
    from gb_automations.clients.frame_auth import get_access_token

    try:
        await get_access_token(force_refresh=True)
        logger.info("⏰ Frame.io token keepalive refresh succeeded")
    except Exception:
        logger.exception("⏰ Frame.io token keepalive refresh failed")


def start_scheduler() -> None:
    """Wire up jobs and start the scheduler. Idempotent — safe to call once at startup."""
    if scheduler.running:
        return

    # Renew Gmail watches every 5 days at 03:17 UTC. The off-hour timing keeps
    # renewal traffic out of usual business windows.
    scheduler.add_job(
        renew_all_watches,
        CronTrigger(day="*/5", hour=3, minute=17),
        id="renew_gmail_watches",
        replace_existing=True,
    )

    # Toggl daily hours aggregator. Gated on the feature flag so a deploy
    # without TOGGL_API_TOKEN / ANSATTE_DB_ID configured doesn't fire a
    # nightly task that's just going to skip itself.
    if settings.sync_toggl_hours:
        scheduler.add_job(
            _enqueue_toggl_hours_job,
            CronTrigger(hour=2, minute=0, timezone="Europe/Oslo"),
            id="toggl_hours_daily",
            replace_existing=True,
        )

    # Fiken graduation hourly poller. Replaces the operator's manual
    # Sjekk fiken click — every hour, scan active projects (anything
    # not at a terminal Faktura status) and enqueue a graduate_faktura
    # task per project. Per-project dedup makes this safe even when
    # the operator clicks Sjekk fiken simultaneously. Gated on the
    # feature flag so a deploy without Fiken doesn't fire an hourly
    # task that just skips itself.
    #
    # Off-minute timing (minute=7) avoids clustering with the
    # daily Toggl + Frame jobs at minute=0/30 — keeps each scheduler
    # tick lightly loaded.
    if settings.sync_fiken_graduations:
        scheduler.add_job(
            _enqueue_fiken_graduations_for_all_active_projects,
            CronTrigger(minute=7),  # every hour at :07
            id="fiken_graduations_hourly",
            replace_existing=True,
        )

    # Frame.io refresh-token keepalive. Adobe rotates on each refresh call;
    # forcing one every day keeps the 14-day expiry window perpetually
    # renewed even when no one touches Frame for a stretch. Gated on
    # frame_client_id so a deploy without Frame configured doesn't fire a
    # job that immediately errors.
    if settings.frame_client_id and settings.frame_client_secret:
        scheduler.add_job(
            _frame_token_keepalive,
            CronTrigger(hour=3, minute=30, timezone="Europe/Oslo"),
            id="refresh_frame_token_daily",
            replace_existing=True,
        )

    scheduler.start()
    logger.info("APScheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
