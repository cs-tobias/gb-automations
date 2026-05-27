"""Sync the Toggl workspace user roster into the local Postgres cache.

Replaces the prior `refresh_ansatte_cache` flow. The Toggl hours engine
needs to map a Toggl `user_id` (the only id Reports v3 returns per entry)
to a Notion user UUID so it can set the `Ansatt` people property. The
lookup happens in two hops:

    toggl_user_id  →  toggl email  →  notion_user_uuid

This module owns the first hop: it reads Toggl's workspace user list and
upserts `TogglUserCache(toggl_user_id PK, email, name)`. The second hop
(email → Notion user UUID) is built in process per sync run by
`sync_toggl_hours._build_notion_user_index`.

Called:
  - At the top of every nightly hours sync, so a new Goldbox hire added
    to Toggl is picked up without a restart.
  - From `POST /debug/toggl/refresh-users` for ad-hoc verification.

Idempotent and additive — re-running is a no-op. Stale rows (user no
longer in the Toggl workspace) get deleted so a departed employee stops
appearing in lookups.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import delete, select

from gb_automations.clients import toggl as toggl_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import TogglUserCache

logger = logging.getLogger(__name__)


@dataclass
class TogglUsersRefreshResult:
    added: int = 0
    updated: int = 0
    removed: int = 0
    skipped_no_email: int = 0
    total_in_toggl: int = 0


async def refresh_toggl_users_cache() -> TogglUsersRefreshResult:
    """Pull Toggl workspace users, upsert into toggl_user_cache, prune missing.

    Returns a small summary the caller (engine or debug route) can log.
    Cheap: workspace member count is tiny.
    """
    result = TogglUsersRefreshResult()

    if not settings.toggl_workspace_id:
        logger.info(
            "toggl users: TOGGL_WORKSPACE_ID not set — skipping cache refresh"
        )
        return result

    users = await toggl_client.list_workspace_users(settings.toggl_workspace_id)
    result.total_in_toggl = len(users)

    # Build {toggl_user_id: (email, name)}. Skip rows without an email —
    # without one we can't perform the Notion-side lookup, so caching is
    # pointless.
    desired: dict[str, tuple[str, str]] = {}
    for u in users:
        toggl_user_id = str(u.get("id", "")).strip()
        if not toggl_user_id:
            continue
        email = (u.get("email") or "").strip().lower()
        if not email:
            result.skipped_no_email += 1
            continue
        name = u.get("name") or u.get("fullname") or ""
        desired[toggl_user_id] = (email, name)

    async with SessionLocal() as session:
        existing_rows = (
            await session.execute(select(TogglUserCache))
        ).scalars().all()
        existing_by_id = {r.toggl_user_id: r for r in existing_rows}

        for toggl_user_id, (email, name) in desired.items():
            current = existing_by_id.get(toggl_user_id)
            if current is None:
                session.add(
                    TogglUserCache(
                        toggl_user_id=toggl_user_id,
                        email=email,
                        name=name,
                    )
                )
                result.added += 1
            elif current.email != email or current.name != name:
                current.email = email
                current.name = name
                result.updated += 1

        stale_ids = set(existing_by_id) - set(desired)
        if stale_ids:
            await session.execute(
                delete(TogglUserCache).where(
                    TogglUserCache.toggl_user_id.in_(stale_ids)
                )
            )
            result.removed += len(stale_ids)

        await session.commit()

    logger.info(
        "toggl users cache refreshed: toggl=%d added=%d updated=%d removed=%d "
        "(skipped %d users with no email)",
        result.total_in_toggl,
        result.added,
        result.updated,
        result.removed,
        result.skipped_no_email,
    )
    return result
