"""Year-partitioned Timer DB router.

Direct parallel to `clients/notion_emails_db.py`. At Goldbox's expected
hours volume (~5 people × ~10 active projects × ~250 working days ≈
12,500 rows/year), a single Notion `Timer` DB would slow down within a
few years. This module partitions hours by calendar year:
`Timer 2026`, `Timer 2027`, etc., each with the same TIMER_PROPS schema.
Queries that span years are rare for timesheet data (people pull a month
or a single year at a time), so per-year DBs keep each one fresh forever.

How `get_timer_db_for_year(2026)` resolves:

  1. In-process cache (per-worker dict) — hit returns immediately.
  2. Postgres cache (`timer_db_cache` table) — populated by prior runs.
  3. Notion search — list child databases under `TOGGL_TIMER_PARENT_PAGE_ID`
     and match by title `Timer YYYY`.
  4. Create — POST `/databases` with the canonical schema from
     `build_timer_db_schema()`.

Self-healing: a cached id that no longer exists in Notion (DB deleted
by hand) is evicted on next use; the engine re-resolves and creates.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.config import build_timer_db_schema, settings
from gb_automations.db import SessionLocal
from gb_automations.models import TimerDbCache

logger = logging.getLogger(__name__)


_IN_PROCESS_CACHE: dict[int, str] = {}


def _timer_db_title(year: int) -> str:
    """Canonical title for a year DB. Used for both search and create."""
    return f"Timer {year}"


def _normalize_id(notion_id: str) -> str:
    """Lowercased, dash-stripped form for cross-format comparison."""
    return notion_id.replace("-", "").lower()


async def get_timer_db_for_year(year: int) -> str:
    """Return the Notion database ID for `Timer {year}`, creating if needed.

    Three-tier lookup: in-process cache → Postgres → Notion. On Notion miss
    we create the DB with the canonical schema. Idempotent — concurrent
    callers may both reach the create step, but Postgres-side upsert collapses
    the race and Notion is fine with two identically-titled DBs (we'd just
    pick the first on next lookup).
    """
    if year in _IN_PROCESS_CACHE:
        return _IN_PROCESS_CACHE[year]

    async with SessionLocal() as session:
        cached_id = await _read_cache(session, year)
        if cached_id:
            # Validate cached id ONCE per process. A stale id would wedge
            # every hours sync for the year with a 404 forever; evict it
            # and re-resolve if gone.
            if await _database_exists(cached_id):
                _IN_PROCESS_CACHE[year] = cached_id
                return cached_id
            logger.warning(
                "Cached Timer DB %s for %d no longer exists in Notion — "
                "evicting stale cache and re-resolving",
                cached_id,
                year,
            )
            await _delete_cache(session, year)
            await session.commit()

        found_id = await _find_in_notion(year)
        if found_id:
            await _write_cache(session, year, found_id)
            await session.commit()
            _IN_PROCESS_CACHE[year] = found_id
            logger.info("Resolved Timer DB for %d via Notion search: %s", year, found_id)
            return found_id

        created_id = await _create_in_notion(year)
        await _write_cache(session, year, created_id)
        await session.commit()
        _IN_PROCESS_CACHE[year] = created_id
        logger.info("Created new Timer DB for %d: %s", year, created_id)
        return created_id


async def all_known_db_ids() -> set[str]:
    """Every Timer DB ID this stack has resolved or created, normalized.

    Currently unused (no Notion-side webhook feedback loop for Timer rows
    yet — the team doesn't edit hours in Notion). Kept for parity with the
    Emails year-router in case a future feature needs it.
    """
    async with SessionLocal() as session:
        result = await session.execute(select(TimerDbCache.notion_db_id))
        return {_normalize_id(db_id) for db_id in result.scalars()}


def reset_cache() -> None:
    """Drop the in-process cache. Useful for tests."""
    _IN_PROCESS_CACHE.clear()


# ============================================================
# Internals
# ============================================================


async def _read_cache(session: AsyncSession, year: int) -> str | None:
    row = await session.get(TimerDbCache, year)
    return row.notion_db_id if row else None


async def _write_cache(session: AsyncSession, year: int, db_id: str) -> None:
    stmt = (
        insert(TimerDbCache)
        .values(year=year, notion_db_id=db_id)
        .on_conflict_do_update(index_elements=["year"], set_={"notion_db_id": db_id})
    )
    await session.execute(stmt)


async def _delete_cache(session: AsyncSession, year: int) -> None:
    row = await session.get(TimerDbCache, year)
    if row is not None:
        await session.delete(row)


async def _database_exists(db_id: str) -> bool:
    """True if the Notion database still exists (and isn't trashed)."""
    from gb_automations.clients.notion import NotionAPIError, _client, _raise_for_status

    async with _client() as client:
        response = await client.get(f"/databases/{db_id}")
    try:
        _raise_for_status(response)
    except NotionAPIError as err:
        if err.status_code == 404:
            return False
        raise
    body = response.json()
    return not (body.get("archived") or body.get("in_trash"))


async def _find_in_notion(year: int) -> str | None:
    """Search Notion for a database titled `Timer {year}`."""
    from gb_automations.clients.notion import _client, _raise_for_status

    target_title = _timer_db_title(year).lower()
    async with _client() as client:
        response = await client.post(
            "/search",
            json={
                "filter": {"value": "database", "property": "object"},
                "page_size": 100,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])

    for db in results:
        title_blocks = db.get("title", [])
        title = "".join(t.get("plain_text", "") for t in title_blocks).strip().lower()
        if title != target_title:
            continue
        # Confirm the parent matches our configured page, when one is set —
        # avoids picking up a like-named DB elsewhere in the workspace.
        parent = db.get("parent") or {}
        if settings.toggl_timer_parent_page_id:
            parent_id = (parent.get("page_id") or "").replace("-", "").lower()
            want = settings.toggl_timer_parent_page_id.replace("-", "").lower()
            if parent_id and parent_id != want:
                continue
        return db["id"]
    return None


async def _create_in_notion(year: int) -> str:
    """Create a new `Timer {year}` database under TOGGL_TIMER_PARENT_PAGE_ID.

    Requires `TOGGL_TIMER_PARENT_PAGE_ID` and `PROJECTS_DB_ID` to be set AND
    the integration to have access to the parent page + the Projects DB.
    `Ansatt` is a `people` property (native Notion users), so no extra DB
    id is needed there.
    """
    if not settings.toggl_timer_parent_page_id:
        raise RuntimeError(
            "TOGGL_TIMER_PARENT_PAGE_ID is not configured — cannot auto-create "
            f"`Timer {year}`. Set it in .env to the Notion page that should "
            "host the year-partitioned Timer databases."
        )
    if not settings.projects_db_id:
        raise RuntimeError(
            "PROJECTS_DB_ID is not configured — cannot auto-create "
            f"`Timer {year}` because Prosjekt is a relation."
        )

    from gb_automations.clients.notion import _client, _raise_for_status

    title = _timer_db_title(year)
    schema = build_timer_db_schema(
        projects_db_id=settings.projects_db_id,
    )
    payload: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": settings.toggl_timer_parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": schema,
    }
    async with _client() as client:
        response = await client.post("/databases", json=payload)
        if response.status_code >= 400:
            logger.error(
                "Failed to create `%s` under page %s: %s",
                title,
                settings.toggl_timer_parent_page_id,
                response.text[:500],
            )
        _raise_for_status(response)
        return response.json()["id"]
