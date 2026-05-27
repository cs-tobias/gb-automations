"""Nightly Toggl Track hours aggregator → Notion `Timer YYYY` DB.

Pulls the last N days (settings.toggl_hours_window_days, default 14) from
Toggl Reports v3, aggregates per (user, project, calendar_day_oslo), and
reconciles the Notion year-DB rows so they exactly match the Toggl
source-of-truth for the window.

Reconciliation model — replace, not merge. For each (user, project, day)
cell in the window:

    Toggl total seconds   Existing Notion row?  →  Action
    -------------------   --------------------     ------
    > 0                   yes, same hours       →  no-op
    > 0                   yes, different hours  →  PATCH hours
    > 0                   no                    →  CREATE row
    0                     yes                   →  ARCHIVE row
    0                     no                    →  no-op

Retroactive edits within the window propagate automatically (the new
Reports v3 totals overwrite whatever was there). Edits to entries older
than the window are NOT picked up — the team accepts this trade-off for
timesheet hygiene (don't edit > 2 weeks back).

Running entries (`stop is None`) are skipped — they're in flight and will
land on tomorrow's run once stopped.

Engine flow:
  1. Refresh Toggl-side user cache (TogglUserCache: id → email).
  2. Build per-run Notion-side user index (email → notion_user_uuid)
     from `GET /v1/users`. Both lookups together resolve every
     toggl_user_id → notion_user_uuid.
  3. Fetch Toggl entries for [today - window_days, today] in workspace tz.
  4. Aggregate to (user_id, project_id, oslo_date) → seconds.
  5. Pre-resolve each aggregated user_id to a notion_user_uuid; cells
     whose user can't be matched (no Notion account with that email)
     are skipped and counted as `skipped_unmatched`.
  6. Group target cells by year (window may straddle Jan 1).
  7. For each year: resolve `Timer YYYY` db, query existing rows in window,
     diff against fresh aggregate, perform creates / patches / archives.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from gb_automations.clients import notion as notion_client
from gb_automations.clients import notion_timer_db
from gb_automations.clients import toggl as toggl_client
from gb_automations.clients.notion import (
    _client as _notion_client,
    _raise_for_status,
    _with_retries,
    archive_page,
)
from gb_automations.config import TIMER_PROPS, settings
from gb_automations.db import SessionLocal
from gb_automations.models import TogglProject, TogglUserCache
from gb_automations.sync.refresh_toggl_users_cache import refresh_toggl_users_cache

logger = logging.getLogger(__name__)


# Aggregation timezone — Goldbox is in Norway, every "what hours did X work
# Tuesday?" question is asked in Oslo time. Toggl returns UTC; we convert
# `start` to this zone to bucket entries to a calendar day.
_OSLO = ZoneInfo("Europe/Oslo")

# Number-property comparison tolerance. Notion stores numbers as floats;
# 7.5 stored ≠ 7.5 read by an FP epsilon every now and then. Hours are
# logged in minutes (= 1/60h) so anything smaller than 0.001h (= 3.6s) is
# noise — never a real change.
_HOURS_EPSILON = 0.001


@dataclass
class TogglHoursResult:
    window_start: str = ""
    window_end: str = ""
    entries_fetched: int = 0
    cells_aggregated: int = 0
    rows_created: int = 0
    rows_updated: int = 0
    rows_archived: int = 0
    skipped_running: int = 0
    # No Notion user account found for this Toggl user's email (mismatch
    # or freelancer without Notion access). The row is dropped silently
    # in production (logged at sync end); operator decides if a fix is
    # needed.
    skipped_unmatched: int = 0
    skipped_unknown_project: int = 0
    skipped_no_project: int = 0
    errors: list[str] = field(default_factory=list)
    action: str = "ok"  # ok | skipped | failed
    note: str | None = None


# ============================================================
# Public entrypoint
# ============================================================


async def sync_toggl_hours() -> TogglHoursResult:
    """Run one nightly hours sync. Called by the queue worker for a
    `toggl_hours_sync` task.

    Returns a populated TogglHoursResult so the worker (and the
    /debug/toggl/sync-hours route) can log a single-line summary.
    Non-fatal per-entry / per-cell problems are counted into the result
    counters and listed in `errors`; only a top-level crash flips
    `action` to "failed".
    """
    result = TogglHoursResult()

    if not settings.sync_toggl_hours:
        result.action = "skipped"
        result.note = "SYNC_TOGGL_HOURS=false"
        return result
    if not settings.toggl_workspace_id:
        result.action = "skipped"
        result.note = "TOGGL_WORKSPACE_ID not set"
        return result
    if not settings.toggl_timer_parent_page_id:
        result.action = "skipped"
        result.note = "TOGGL_TIMER_PARENT_PAGE_ID not set"
        return result

    # Refresh both sides of the user map before doing anything else. A
    # workspace member added to Toggl OR to Notion today should be
    # synced for today, not "tomorrow once you remember to restart."
    try:
        await refresh_toggl_users_cache()
    except Exception as err:
        logger.exception("toggl hours: toggl users refresh failed")
        result.errors.append(f"toggl users refresh failed: {err}")
        # Continue with whatever's in the cache — better to write rows for
        # known users than to fail the whole sync.

    window_days = max(1, settings.toggl_hours_window_days)
    today_oslo = datetime.now(_OSLO).date()
    window_start = today_oslo - timedelta(days=window_days - 1)
    result.window_start = window_start.isoformat()
    result.window_end = today_oslo.isoformat()

    # Fetch entries from Toggl. Reports v3 expects YYYY-MM-DD and treats
    # the range inclusively. We pass Oslo-local dates: Toggl filters by
    # `start_date <= entry.start <= end_date + 1d` (per their docs) which
    # means an entry that STARTED at 23:59 UTC on Dec 31 (= 00:59 Oslo
    # Jan 1) will be returned for an end_date of Dec 31. The local-day
    # bucketing happens below in _aggregate, so this is fine — we'd
    # rather see slightly too many entries and filter than miss some.
    try:
        entries = await toggl_client.search_time_entries(
            settings.toggl_workspace_id,
            start_date=window_start.isoformat(),
            end_date=today_oslo.isoformat(),
        )
    except Exception as err:
        logger.exception("toggl hours: Reports v3 fetch failed")
        result.action = "failed"
        result.note = f"Reports v3 fetch failed: {err}"
        return result
    result.entries_fetched = len(entries)
    logger.info(
        "toggl hours: fetched %d entries for window %s … %s",
        len(entries),
        result.window_start,
        result.window_end,
    )

    # Aggregate to (toggl_user_id, toggl_project_id, oslo_date) → seconds.
    aggregate, agg_stats = _aggregate(entries)
    result.cells_aggregated = len(aggregate)
    result.skipped_running = agg_stats["skipped_running"]
    result.skipped_no_project = agg_stats["skipped_no_project"]

    # Resolve user + project ids once, up front. The user resolution is
    # two-hop: TogglUserCache gives us the toggl email, then the Notion
    # workspace user list gives us the user UUID for that email. A cell
    # we can't resolve gets skipped (counted) instead of erroring — keeps
    # the engine forgiving of new Toggl members not yet on Notion, or a
    # Toggl project not yet mirrored to a Notion project.
    toggl_user_emails = await _load_toggl_user_emails()
    try:
        notion_user_by_email = await _build_notion_user_index()
    except Exception as err:
        logger.exception("toggl hours: Notion users fetch failed")
        result.action = "failed"
        result.note = f"Notion users fetch failed: {err}"
        return result
    known_project_map = await _load_toggl_project_map()

    # Pre-resolve toggl_user_id → notion_user_uuid for every user that
    # actually appears in this window's entries. One pass; cells whose
    # user can't be resolved get logged once at debug here and counted
    # under skipped_unmatched in the cell loop.
    user_uuid_lookup: dict[str, str] = {}
    user_name_lookup: dict[str, str] = {}
    for user_id in {key[0] for key in aggregate}:
        email = (toggl_user_emails.get(user_id) or {}).get("email", "")
        notion_uuid = notion_user_by_email.get(email.lower()) if email else None
        if notion_uuid:
            user_uuid_lookup[user_id] = notion_uuid
            user_name_lookup[user_id] = (
                toggl_user_emails.get(user_id, {}).get("name", "")
            )
        else:
            logger.debug(
                "toggl hours: no Notion user for toggl_user_id=%s email=%r",
                user_id,
                email,
            )

    # Group cells by year so we can resolve the correct Timer YYYY DB
    # (the window may straddle Jan 1). Cells with no matching Notion
    # user or with an unmirrored Toggl project are skipped here.
    cells_by_year: dict[int, dict[tuple[str, str, date], int]] = defaultdict(dict)
    for key, seconds in aggregate.items():
        user_id, project_id, oslo_date = key
        if user_id not in user_uuid_lookup:
            result.skipped_unmatched += 1
            continue
        if project_id not in known_project_map:
            result.skipped_unknown_project += 1
            continue
        cells_by_year[oslo_date.year][key] = seconds

    # Process each year independently. A year boundary is rare (once a
    # year) and each year DB is independent in Notion, so per-year is the
    # cleanest division — no cross-DB queries needed.
    for year, year_cells in cells_by_year.items():
        try:
            await _reconcile_year(
                year,
                year_cells,
                window_start=window_start,
                window_end=today_oslo,
                user_uuid_lookup=user_uuid_lookup,
                user_name_lookup=user_name_lookup,
                project_map=known_project_map,
                result=result,
            )
        except Exception as err:
            logger.exception(
                "toggl hours: year %d reconcile crashed", year
            )
            result.errors.append(f"year {year} reconcile failed: {err}")

    logger.info(
        "toggl hours sync done: created=%d updated=%d archived=%d "
        "(skipped: running=%d unmatched=%d unknown_project=%d "
        "no_project=%d) errors=%d",
        result.rows_created,
        result.rows_updated,
        result.rows_archived,
        result.skipped_running,
        result.skipped_unmatched,
        result.skipped_unknown_project,
        result.skipped_no_project,
        len(result.errors),
    )
    return result


# ============================================================
# Aggregation
# ============================================================


def _aggregate(
    entries: list[dict[str, Any]],
) -> tuple[dict[tuple[str, str, date], int], dict[str, int]]:
    """Aggregate Toggl Reports v3 entries to (user, project, oslo_day) → seconds.

    Returns the aggregate plus a small stats dict for the engine's counters.

    Toggl Reports v3 entries that we care about look roughly like:
        {
            "user_id": 13166188,
            "project_id": 198765432,
            "seconds": 5400,
            "start": "2026-05-26T08:30:00+00:00",
            "stop":  "2026-05-26T10:00:00+00:00",
            ...
        }

    `seconds` is straightforward when stop is set. If `stop` is null the
    entry is still running — we skip and pick it up tomorrow.

    Entries without a project_id (someone tracked time with no project
    selected) are skipped — there's no way to attribute them to a Notion
    row, and the team's intent in that case is "not for billing."
    """
    out: dict[tuple[str, str, date], int] = defaultdict(int)
    stats = {"skipped_running": 0, "skipped_no_project": 0}

    for e in entries:
        if e.get("stop") is None:
            stats["skipped_running"] += 1
            continue
        project_id = e.get("project_id")
        if project_id is None:
            stats["skipped_no_project"] += 1
            continue
        seconds = e.get("seconds")
        if not isinstance(seconds, (int, float)) or seconds <= 0:
            continue
        user_id = str(e.get("user_id", ""))
        if not user_id:
            continue

        start_raw = e.get("start") or ""
        try:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        # If Toggl omits the tz (unlikely on Reports v3 but defensive),
        # assume UTC — that's what their docs claim everywhere.
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
        oslo_date = start_dt.astimezone(_OSLO).date()

        key = (user_id, str(project_id), oslo_date)
        out[key] += int(seconds)

    return out, stats


# ============================================================
# Cache loads
# ============================================================


async def _load_toggl_user_emails() -> dict[str, dict[str, str]]:
    """toggl_user_id → {email, name} for every cached Toggl workspace user.

    Cached by `refresh_toggl_users_cache` at the top of every sync run.
    Email is lowercased at write time, so callers should also lowercase
    on the lookup side for Notion match.
    """
    async with SessionLocal() as session:
        rows = (await session.execute(select(TogglUserCache))).scalars()
        return {
            row.toggl_user_id: {"email": row.email, "name": row.name}
            for row in rows
        }


async def _build_notion_user_index() -> dict[str, str]:
    """Lowercased email → Notion user UUID for every workspace member.

    Built per-sync run (small list, ~5-10 users; no need to persist).
    Filters to `type=="person"` — bot users have no `person.email` and
    can't own Timer rows. A duplicate email (shouldn't happen but
    defensive) gets the FIRST matching user; the engine's behavior at
    that point is "predictable, not perfect."
    """
    index: dict[str, str] = {}
    users = await notion_client.list_workspace_users()
    for u in users:
        if u.get("type") != "person":
            continue
        person = u.get("person") or {}
        email = (person.get("email") or "").strip().lower()
        if not email:
            continue
        index.setdefault(email, u["id"])
    return index


async def _load_toggl_project_map() -> dict[str, str]:
    """toggl_project_id → notion_page_id (Projects DB) for every mirrored project."""
    async with SessionLocal() as session:
        rows = (await session.execute(select(TogglProject))).scalars()
        return {row.toggl_project_id: row.notion_page_id for row in rows}


# ============================================================
# Per-year reconciliation
# ============================================================


async def _reconcile_year(
    year: int,
    cells: dict[tuple[str, str, date], int],
    *,
    window_start: date,
    window_end: date,
    user_uuid_lookup: dict[str, str],
    user_name_lookup: dict[str, str],
    project_map: dict[str, str],
    result: TogglHoursResult,
) -> None:
    """Reconcile one year's window: query existing Notion rows in [window_start,
    window_end] ∩ year, diff against `cells`, perform create/patch/archive.
    """
    db_id = await notion_timer_db.get_timer_db_for_year(year)

    # Year-clipped window for the Notion query — if the window straddles
    # Jan 1 we only ask each year-DB about its own slice.
    year_start = max(window_start, date(year, 1, 1))
    year_end = min(window_end, date(year, 12, 31))

    existing = await _query_timer_rows_in_window(
        db_id, year_start, year_end
    )

    # Index existing rows by (user_id, project_id, date) so the diff is a
    # straight dict lookup. A duplicate cell (shouldn't happen but
    # someone may have copy-pasted a row) is collapsed — the FIRST row
    # wins and the rest get archived.
    existing_by_key: dict[tuple[str, str, date], dict[str, Any]] = {}
    duplicate_rows: list[dict[str, Any]] = []
    for row in existing:
        key = _row_key(row)
        if key is None:
            continue
        if key in existing_by_key:
            duplicate_rows.append(row)
            continue
        existing_by_key[key] = row

    # Archive duplicates first so the subsequent diff sees only unique keys.
    for dup in duplicate_rows:
        try:
            await archive_page(dup["id"])
            result.rows_archived += 1
            logger.info(
                "toggl hours: archived duplicate row %s (%s)",
                dup["id"],
                _row_key(dup),
            )
        except Exception as err:
            result.errors.append(f"archive duplicate failed: {err}")

    # Diff: walk every cell in the window that EITHER has Toggl seconds
    # OR has an existing Notion row, so we cover creates, updates, and
    # archives in one loop.
    all_keys = set(cells) | set(existing_by_key)
    for key in all_keys:
        seconds = cells.get(key, 0)
        row = existing_by_key.get(key)
        try:
            if seconds <= 0 and row is not None:
                await archive_page(row["id"])
                result.rows_archived += 1
                continue
            if seconds <= 0:
                continue
            hours = round(seconds / 3600.0, 2)
            if row is None:
                await _create_timer_row(
                    db_id=db_id,
                    key=key,
                    hours=hours,
                    user_uuid_lookup=user_uuid_lookup,
                    user_name_lookup=user_name_lookup,
                    project_map=project_map,
                )
                result.rows_created += 1
            else:
                current = _read_number_prop(row, TIMER_PROPS["hours"]) or 0.0
                if abs(current - hours) > _HOURS_EPSILON:
                    await _patch_timer_hours(row["id"], hours)
                    result.rows_updated += 1
        except Exception as err:
            logger.exception(
                "toggl hours: cell %s failed", key
            )
            result.errors.append(f"cell {key}: {err}")


# ============================================================
# Notion read / write helpers
# ============================================================


async def _query_timer_rows_in_window(
    db_id: str, window_start: date, window_end: date
) -> list[dict[str, Any]]:
    """Pull every non-archived Timer row whose Dato is in [start, end]."""
    rows: list[dict[str, Any]] = []
    start_cursor: str | None = None
    body_base: dict[str, Any] = {
        "filter": {
            "property": TIMER_PROPS["date"],
            "date": {
                "on_or_after": window_start.isoformat(),
                "on_or_before": window_end.isoformat(),
            },
        },
        "page_size": 100,
    }
    async with _notion_client() as client:
        while True:
            body = dict(body_base)
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await _with_retries(
                lambda b=body: client.post(
                    f"/databases/{db_id}/query", json=b
                ),
                op_name=f"POST /databases/{db_id}/query timer-window",
            )
            _raise_for_status(response)
            payload = response.json()
            rows.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:
                break
    return [r for r in rows if not r.get("archived") and not r.get("in_trash")]


def _row_key(row: dict[str, Any]) -> tuple[str, str, date] | None:
    """Extract (toggl_user_id, toggl_project_id, dato) from a Timer row.

    Returns None if any of the three is missing/malformed — those rows
    are treated as user-managed (not ours) and ignored by the engine.
    """
    props = row.get("properties") or {}
    user_id = _read_rich_text(props, TIMER_PROPS["toggl_user_id"])
    project_id = _read_rich_text(props, TIMER_PROPS["toggl_project_id"])
    date_str = _read_date_prop(props, TIMER_PROPS["date"])
    if not user_id or not project_id or not date_str:
        return None
    try:
        d = date.fromisoformat(date_str[:10])
    except ValueError:
        return None
    return (user_id, project_id, d)


def _read_rich_text(props: dict[str, Any], name: str) -> str:
    prop = props.get(name) or {}
    blocks = prop.get("rich_text") or []
    return "".join(b.get("plain_text", "") for b in blocks).strip()


def _read_date_prop(props: dict[str, Any], name: str) -> str | None:
    prop = props.get(name) or {}
    date_obj = prop.get("date") or {}
    return date_obj.get("start")


def _read_number_prop(row: dict[str, Any], name: str) -> float | None:
    prop = (row.get("properties") or {}).get(name) or {}
    return prop.get("number")


async def _create_timer_row(
    *,
    db_id: str,
    key: tuple[str, str, date],
    hours: float,
    user_uuid_lookup: dict[str, str],
    user_name_lookup: dict[str, str],
    project_map: dict[str, str],
) -> None:
    """Create one Timer row. Caller has already verified user + project
    are resolvable via the lookups."""
    user_id, project_id, dato = key
    notion_user_uuid = user_uuid_lookup[user_id]
    user_name = user_name_lookup.get(user_id, "")
    notion_project_page = project_map[project_id]
    title = f"{user_name or user_id} — {dato.isoformat()}"

    properties: dict[str, Any] = {
        TIMER_PROPS["name"]: {
            "title": [{"text": {"content": title}}]
        },
        TIMER_PROPS["date"]: {"date": {"start": dato.isoformat()}},
        TIMER_PROPS["employee"]: {
            "people": [{"object": "user", "id": notion_user_uuid}]
        },
        TIMER_PROPS["project"]: {
            "relation": [{"id": notion_project_page}]
        },
        TIMER_PROPS["hours"]: {"number": hours},
        TIMER_PROPS["toggl_user_id"]: {
            "rich_text": [{"text": {"content": user_id}}]
        },
        TIMER_PROPS["toggl_project_id"]: {
            "rich_text": [{"text": {"content": project_id}}]
        },
    }

    async with _notion_client() as client:
        response = await _with_retries(
            lambda: client.post(
                "/pages",
                json={
                    "parent": {"database_id": db_id},
                    "properties": properties,
                },
            ),
            op_name=f"POST /pages timer({user_id},{project_id},{dato})",
        )
        _raise_for_status(response)


async def _patch_timer_hours(page_id: str, hours: float) -> None:
    """Patch only the Timer hours — leaves relations / ids untouched."""
    async with _notion_client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{page_id}",
                json={
                    "properties": {
                        TIMER_PROPS["hours"]: {"number": hours},
                    }
                },
            ),
            op_name=f"PATCH /pages/{page_id} timer-hours",
        )
        _raise_for_status(response)
