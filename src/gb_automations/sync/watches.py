"""Gmail Pub/Sub watch management.

Functions:
  - start_watch_for_user: call Gmail users.watch() for one user, save its baseline
    historyId to sync_cursors so subsequent push deliveries have a starting point.
  - renew_all_watches: call start_watch for every active user. Run weekly because
    Gmail watches expire after 7 days.

Used by:
  - scripts/start_watches.py (one-off CLI for initial setup or manual recovery)
  - jobs/scheduler.py (APScheduler weekly job)
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import SyncCursor, User

logger = logging.getLogger(__name__)

GMAIL_CURSOR_SOURCE_PREFIX = "gmail_history:"


def cursor_source_for(email: str) -> str:
    return f"{GMAIL_CURSOR_SOURCE_PREFIX}{email.lower()}"


async def start_watch_for_user(user_email: str) -> dict:
    """Call users.watch() for one user. Saves the returned historyId as the baseline."""
    if not settings.pubsub_topic:
        raise RuntimeError("PUBSUB_TOPIC not configured")

    response = await asyncio.to_thread(gmail_client.start_watch, user_email, settings.pubsub_topic)
    history_id = str(response.get("historyId") or "")
    expiration = response.get("expiration")  # ms-since-epoch string

    if history_id:
        async with SessionLocal() as session:
            stmt = (
                pg_insert(SyncCursor)
                .values(source=cursor_source_for(user_email), cursor_value=history_id)
                .on_conflict_do_update(index_elements=["source"], set_={"cursor_value": history_id})
            )
            await session.execute(stmt)
            await session.commit()

    logger.info(
        "Started Gmail watch for %s: historyId=%s expires=%s",
        user_email,
        history_id,
        expiration,
    )
    return {"email": user_email, "history_id": history_id, "expiration": expiration}


async def renew_all_watches() -> list[dict]:
    """Call start_watch_for_user for every active user. Idempotent — Gmail extends
    the existing watch if one is in place, or creates a new one if expired.
    """
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()

    results = []
    for user in users:
        try:
            result = await start_watch_for_user(user.email)
            results.append({"email": user.email, **result, "ok": True})
        except Exception as err:
            logger.exception("Failed to renew watch for %s", user.email)
            results.append({"email": user.email, "ok": False, "error": str(err)})
    return results
