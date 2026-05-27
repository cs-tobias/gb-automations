"""APScheduler setup — runs scheduled background jobs inside the FastAPI process.

Currently:
  - Weekly Gmail watch renewal. Watches expire after 7 days; we renew every 5 days
    to leave headroom in case a run is missed.
  - Daily Toggl hours aggregation (when SYNC_TOGGL_HOURS=true). Fires at
    02:00 Europe/Oslo so the previous day's entries are settled before the
    pull. Only ENQUEUES a task — the durable worker runs the actual sync,
    so a long Reports v3 call can't trip the scheduler's misfire timeout.

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

    scheduler.start()
    logger.info("APScheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
