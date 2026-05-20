"""Year-partitioned Emails DB router.

At Goldbox volume (~7k emails/year), a single Notion `E-post` database starts
to feel slow past year 3 and gets actively painful by year 5+. This module
partitions Emails by calendar year: `E-post 2024`, `E-post 2025`, `E-post 2026`,
etc., each with the same schema. Threads that span years stay linked via the
`Thread ID` text property (same value across year DBs) so Notion linked-DB
views can group them.

How the resolution works for `get_emails_db_for_year(2026)`:

  1. In-process cache (per-worker dict) — hit returns immediately.
  2. Postgres cache (`emails_db_cache` table) — populated by prior runs and
     by other workers. On hit, also fills the in-process cache.
  3. Notion search — list child databases under `EMAILS_PARENT_PAGE_ID` and
     match by title `E-post YYYY`. On hit, persist to Postgres + in-process.
  4. Create — POST `/databases` with the canonical schema cloned from
     `build_emails_db_schema()`. Persist on success.

Feedback-loop filtering uses `is_known_emails_db_id()` so the Notion webhook
can recognize "our own writes" across every year DB without per-DB env config.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.config import EMAIL_TAGS, build_emails_db_schema, settings
from gb_automations.db import SessionLocal
from gb_automations.models import EmailsDbCache

logger = logging.getLogger(__name__)


_IN_PROCESS_CACHE: dict[int, str] = {}


def _emails_db_title(year: int) -> str:
    """Canonical title for a year DB. Used for both search and create.

    Norwegian to match the other Goldbox DBs (Kontaktpersoner, Kunder). Note:
    existing `Emails YYYY` DBs created before this rename won't be found by the
    Notion-search fallback anymore, but the Postgres `emails_db_cache` keys by
    year (not title), so already-resolved years keep working untouched.
    """
    return f"E-post {year}"


def _normalize_id(notion_id: str) -> str:
    """Lowercased, dash-stripped form for cross-format comparison."""
    return notion_id.replace("-", "").lower()


async def get_emails_db_for_year(year: int) -> str:
    """Return the Notion database ID for `Emails {year}`, creating if needed.

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
            _IN_PROCESS_CACHE[year] = cached_id
            return cached_id

        # Look in Notion. The integration may have been pointed at a parent
        # page that ALREADY has year DBs (set up by a prior deployment or by
        # hand) — pick those up before creating duplicates.
        found_id = await _find_in_notion(year)
        if found_id:
            await _write_cache(session, year, found_id)
            await session.commit()
            _IN_PROCESS_CACHE[year] = found_id
            logger.info("Resolved Emails DB for %d via Notion search: %s", year, found_id)
            return found_id

        created_id = await _create_in_notion(year)
        await _write_cache(session, year, created_id)
        await session.commit()
        _IN_PROCESS_CACHE[year] = created_id
        logger.info("Created new Emails DB for %d: %s", year, created_id)
        return created_id


async def all_known_db_ids() -> set[str]:
    """Every year-DB ID this stack has resolved or created, normalized for compare.

    Used by the Notion webhook to recognize our own writes across all year DBs.
    Refreshed lazily — callers that need up-to-date data should expect a DB
    created right now by ANOTHER worker to take a few seconds to appear here.
    """
    async with SessionLocal() as session:
        result = await session.execute(select(EmailsDbCache.notion_db_id))
        return {_normalize_id(db_id) for db_id in result.scalars()}


def is_known_emails_db_id(db_id: str, known_ids: set[str]) -> bool:
    """True iff `db_id` matches any year Emails DB we've seen.

    Caller passes the pre-fetched `known_ids` set so this stays sync-friendly
    inside the webhook fast-path. Comparison is dash-stripped + lowercased.
    """
    return _normalize_id(db_id) in known_ids


def reset_cache() -> None:
    """Drop the in-process cache. Useful for tests; production rarely needs it."""
    _IN_PROCESS_CACHE.clear()


# ============================================================
# Internals
# ============================================================


async def _read_cache(session: AsyncSession, year: int) -> str | None:
    row = await session.get(EmailsDbCache, year)
    return row.notion_db_id if row else None


async def _write_cache(session: AsyncSession, year: int, db_id: str) -> None:
    stmt = (
        insert(EmailsDbCache)
        .values(year=year, notion_db_id=db_id)
        .on_conflict_do_update(index_elements=["year"], set_={"notion_db_id": db_id})
    )
    await session.execute(stmt)


async def _find_in_notion(year: int) -> str | None:
    """Search Notion for a database titled `Emails {year}`.

    Uses the /search endpoint filtered to databases. Notion's search is
    eventual-consistency for very-recently-created databases (~1-2s lag), but
    that only matters if we just created one and immediately look it up —
    in practice we always cache locally on create so this path doesn't fire.
    """
    from gb_automations.clients.notion import _client, _raise_for_status

    target_title = _emails_db_title(year).lower()
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
        if settings.emails_parent_page_id:
            parent_id = (parent.get("page_id") or "").replace("-", "").lower()
            want = settings.emails_parent_page_id.replace("-", "").lower()
            if parent_id and parent_id != want:
                continue
        return db["id"]
    return None


async def _create_in_notion(year: int) -> str:
    """Create a new `Emails {year}` database under EMAILS_PARENT_PAGE_ID.

    Requires `EMAILS_PARENT_PAGE_ID` to be set AND the integration to have
    access to that page. The Project relation requires `PROJECTS_DB_ID` so
    a fresh deployment must configure both before the first email arrives.
    """
    if not settings.emails_parent_page_id:
        raise RuntimeError(
            "EMAILS_PARENT_PAGE_ID is not configured — cannot auto-create "
            f"`Emails {year}`. Set it in .env to the Notion page that should "
            "host the year-partitioned Emails databases."
        )
    if not settings.projects_db_id:
        raise RuntimeError(
            "PROJECTS_DB_ID is not configured — cannot auto-create "
            f"`Emails {year}` because the Project property is a relation."
        )
    if not settings.contacts_db_id:
        raise RuntimeError(
            "CONTACTS_DB_ID is not configured — cannot auto-create "
            f"`Emails {year}` because From/To/Cc are relations to Contacts."
        )

    from gb_automations.clients.notion import _client, _raise_for_status

    title = _emails_db_title(year)
    schema = build_emails_db_schema(
        projects_db_id=settings.projects_db_id,
        contacts_db_id=settings.contacts_db_id,
        tag_options=EMAIL_TAGS,
    )
    payload: dict[str, Any] = {
        "parent": {"type": "page_id", "page_id": settings.emails_parent_page_id},
        "title": [{"type": "text", "text": {"content": title}}],
        "properties": schema,
    }
    async with _client() as client:
        response = await client.post("/databases", json=payload)
        if response.status_code >= 400:
            logger.error(
                "Failed to create `%s` under page %s: %s",
                title,
                settings.emails_parent_page_id,
                response.text[:500],
            )
        _raise_for_status(response)
        return response.json()["id"]
