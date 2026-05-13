"""APScheduler setup — runs scheduled background jobs inside the FastAPI process.

Currently:
  - Weekly Gmail watch renewal. Watches expire after 7 days; we renew every 5 days
    to leave headroom in case a run is missed.

The scheduler is started in main.py's lifespan handler and shut down on app exit.
"""

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from gb_automations.sync.watches import renew_all_watches

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler()


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
    scheduler.start()
    logger.info("APScheduler started with jobs: %s", [j.id for j in scheduler.get_jobs()])


def shutdown_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("APScheduler shut down")
