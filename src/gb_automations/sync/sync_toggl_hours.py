"""Toggl Track hours aggregator → Notion `Timer YYYY` DB (the time-bank).

Pulls the configured window (settings.toggl_hours_window_days, default 32)
from Toggl Reports v3, aggregates per (user, project, calendar_day_oslo),
and reconciles the Notion year-DB rows so they exactly match the Toggl
source-of-truth for the window.

Goldbox uses Notion as the SOURCE OF TRUTH for the company's complete
time-bank, so this engine writes EVERY Toggl entry (including ones with
no project, with a project not yet mirrored, or on a template/internal
project). The Notion `Prosjekt` relation is set when a TogglProject
mapping exists; otherwise the row lands with an empty relation and the
Toggl project name stored in the `Toggl Prosjekt navn` column.

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
    # Informational: cells where the Toggl project_id has no Notion
    # relation mapped. These are NOT skipped — they still land in Notion
    # with an empty relation and Toggl Prosjekt navn populated. Counter
    # exists so the team can see how many rows are unlinked.
    unmatched_project_kept: int = 0
    # Informational: cells where Toggl reported no project at all
    # (project_id=null). Also kept — written with relation empty and
    # Toggl Prosjekt navn = "Uten prosjekt".
    no_project_kept: int = 0
    errors: list[str] = field(default_factory=list)
    action: str = "ok"  # ok | skipped | failed
    note: str | None = None


# ============================================================
# Public entrypoint
# ============================================================


async def sync_toggl_hours(
    *,
    window_start: date | None = None,
    window_end: date | None = None,
) -> TogglHoursResult:
    """Run one hours sync. Default mode (no args) is the nightly window:
    last `settings.toggl_hours_window_days` days ending today (Oslo) —
    used by the queue worker for a `toggl_hours_sync` task.

    `window_start` and `window_end` override the date range for one-shot
    backfills (e.g. "everything since Jan 1"). Both inclusive, Oslo-local
    dates. The engine's reconciliation logic doesn't care how wide the
    range is — it diffs Notion against Toggl within whatever window it's
    given, so a 6-month backfill and a 14-day nightly use the exact same
    code path.

    Returns a populated TogglHoursResult so the worker (and the
    /debug/toggl/sync-hours / /debug/toggl/backfill routes) can log a
    single-line summary. Non-fatal per-entry / per-cell problems are
    counted into the result counters and listed in `errors`; only a
    top-level crash flips `action` to "failed".
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

    today_oslo = datetime.now(_OSLO).date()
    if window_end is None:
        window_end = today_oslo
    if window_start is None:
        window_days = max(1, settings.toggl_hours_window_days)
        window_start = window_end - timedelta(days=window_days - 1)
    if window_start > window_end:
        result.action = "failed"
        result.note = (
            f"window_start ({window_start}) is after window_end ({window_end})"
        )
        return result
    result.window_start = window_start.isoformat()
    result.window_end = window_end.isoformat()

    # Fetch entries from Toggl. Reports v3 expects YYYY-MM-DD and treats
    # the range inclusively. We pass Oslo-local dates: Toggl filters by
    # `start_date <= entry.start <= end_date + 1d` (per their docs) which
    # means an entry that STARTED at 23:59 UTC on Dec 31 (= 00:59 Oslo
    # Jan 1) will be returned for an end_date of Dec 31. The local-day
    # bucketing happens below in _aggregate, so this is fine — we'd
    # rather see slightly too many entries and filter than miss some.
    #
    # Toggl Reports v3 enforces a 1-year max range per call. For multi-
    # year backfills, fetch year-by-year and concatenate before aggregation.
    try:
        entries = await _fetch_entries_multi_year(
            settings.toggl_workspace_id, window_start, window_end
        )
    except Exception as err:
        logger.exception("toggl hours: Reports v3 fetch failed")
        result.action = "failed"
        result.note = f"Reports v3 fetch failed: {err}"
        return result
    # Reports v3 returns grouped records (one per user×project) with a
    # nested time_entries array — count the flat entries for an accurate
    # log line, since "17 entries" reading "17 groups" was a real source
    # of confusion the first time this code ran.
    individual_count = sum(len(g.get("time_entries") or []) for g in entries)
    result.entries_fetched = individual_count
    logger.info(
        "toggl hours: fetched %d entries (in %d groups) for window %s … %s",
        individual_count,
        len(entries),
        result.window_start,
        result.window_end,
    )

    # Aggregate to (toggl_user_id, toggl_project_id, oslo_date) → seconds.
    # Empty-string project_id = "tracked without a project" — kept, not skipped.
    aggregate, descriptions, agg_stats = _aggregate(entries)
    result.cells_aggregated = len(aggregate)
    result.skipped_running = agg_stats["skipped_running"]
    result.no_project_kept = agg_stats["no_project_cells"]

    # Resolve user + project ids once, up front. The user resolution is
    # two-hop: TogglUserCache gives us the toggl email, then the Notion
    # workspace user list gives us the user UUID for that email. A cell
    # we can't resolve gets skipped (counted) instead of erroring — keeps
    # the engine forgiving of new Toggl members not yet on Notion, or a
    # Toggl project not yet mirrored to a Notion project.
    toggl_user_emails = await _load_toggl_user_emails()
    # Dev-only email rewrite: when a developer's Toggl email differs from
    # their Notion email (production Goldbox accounts match, so this is a
    # no-op there), rewrite the lookup-side email before the Notion match
    # so the engine attributes hours to the right Notion user. Leaves the
    # Toggl side (display name, cache key) untouched.
    overrides = settings.toggl_dev_email_overrides_map
    if overrides:
        logger.info(
            "toggl hours: applying %d dev email override(s): %s",
            len(overrides),
            ", ".join(f"{k}→{v}" for k, v in overrides.items()),
        )
        for user_id, info in toggl_user_emails.items():
            mapped = overrides.get(info["email"].lower())
            if mapped:
                info["email"] = mapped
    try:
        notion_user_by_email = await _build_notion_user_index()
    except Exception as err:
        logger.exception("toggl hours: Notion users fetch failed")
        result.action = "failed"
        result.note = f"Notion users fetch failed: {err}"
        return result
    known_project_map = await _load_toggl_project_map()

    # Build toggl_project_id → name lookup for the `Toggl Prosjekt navn`
    # column. We always write the Toggl name so unmatched rows are still
    # attributable (and matched rows show what Toggl had vs Notion).
    # Fetch failures are non-fatal — the column just gets the project id
    # as fallback, which is still useful for debugging.
    toggl_project_names: dict[str, str] = {}
    try:
        toggl_projects = await toggl_client.list_projects(
            settings.toggl_workspace_id
        )
        for p in toggl_projects:
            pid = p.get("id")
            name = p.get("name") or ""
            if pid is not None and name:
                toggl_project_names[str(pid)] = name
    except Exception as err:
        logger.warning(
            "toggl hours: list_projects failed (%s) — rows will use "
            "project_id as fallback for Toggl Prosjekt navn",
            err,
        )

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
    # (the window may straddle Jan 1). Only cells whose user can't be
    # matched to a Notion workspace member are dropped — everything else
    # flows through (no project / unmatched project = still written,
    # relation just stays empty).
    cells_by_year: dict[int, dict[tuple[str, str, date], int]] = defaultdict(dict)
    for key, seconds in aggregate.items():
        user_id, project_id, oslo_date = key
        if user_id not in user_uuid_lookup:
            result.skipped_unmatched += 1
            continue
        if project_id and project_id not in known_project_map:
            # Not skipped — just counted so the operator can see how many
            # rows landed without a Notion-project relation.
            result.unmatched_project_kept += 1
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
                window_end=window_end,
                user_uuid_lookup=user_uuid_lookup,
                user_name_lookup=user_name_lookup,
                project_map=known_project_map,
                toggl_project_names=toggl_project_names,
                descriptions=descriptions,
                result=result,
            )
        except Exception as err:
            logger.exception(
                "toggl hours: year %d reconcile crashed", year
            )
            result.errors.append(f"year {year} reconcile failed: {err}")

    logger.info(
        "toggl hours sync done: created=%d updated=%d archived=%d "
        "(skipped running=%d skipped unmatched_user=%d "
        "unmatched_project_kept=%d no_project_kept=%d) errors=%d",
        result.rows_created,
        result.rows_updated,
        result.rows_archived,
        result.skipped_running,
        result.skipped_unmatched,
        result.unmatched_project_kept,
        result.no_project_kept,
        len(result.errors),
    )
    return result


# ============================================================
# Toggl fetch — multi-year safe
# ============================================================


async def _fetch_entries_multi_year(
    workspace_id: str, window_start: date, window_end: date
) -> list[dict[str, Any]]:
    """Fetch Toggl Reports v3 entries across an arbitrary window.

    Reports v3 caps a single call at a 1-year range. For multi-year
    backfills we slice the window into per-calendar-year chunks and
    concatenate the grouped records. Same-day single-year calls (the
    normal nightly case) take exactly one underlying call.
    """
    if window_start.year == window_end.year:
        return await toggl_client.search_time_entries(
            workspace_id,
            start_date=window_start.isoformat(),
            end_date=window_end.isoformat(),
        )

    all_entries: list[dict[str, Any]] = []
    for year in range(window_start.year, window_end.year + 1):
        slice_start = max(window_start, date(year, 1, 1))
        slice_end = min(window_end, date(year, 12, 31))
        logger.info(
            "toggl hours: multi-year fetch %s … %s",
            slice_start.isoformat(),
            slice_end.isoformat(),
        )
        chunk = await toggl_client.search_time_entries(
            workspace_id,
            start_date=slice_start.isoformat(),
            end_date=slice_end.isoformat(),
        )
        all_entries.extend(chunk)
    return all_entries


# ============================================================
# Aggregation
# ============================================================


def _aggregate(
    entries: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str, date], int],
    dict[tuple[str, str, date], set[str]],
    dict[str, int],
]:
    """Aggregate Toggl Reports v3 entries to (user, project, oslo_day) cells.

    Returns:
        - seconds_by_key: cell → total seconds
        - descriptions_by_key: cell → set of unique non-empty descriptions
        - stats: per-run counters

    The project_id slot in the key is `""` when the Toggl entry has no
    project (project_id=null). Empty-project cells still flow through the
    pipeline; only the Notion relation is left empty downstream.

    Toggl Reports v3's `search/time_entries` returns GROUPED records — one
    per (user, project, description), with a nested `time_entries` array
    of the actual individual sessions sharing that description:
        {
            "user_id": 13166188,
            "project_id": 198765432 | null,
            "description": "design review",     # at GROUP level, not nested
            "time_entries": [
                {"id": ..., "seconds": 5400,
                 "start": "2026-05-26T08:30:00+02:00",
                 "stop":  "2026-05-26T10:00:00+02:00"},
                ...
            ],
            ...
        }

    A nested entry with `stop is None` is still running — skip just that
    one; the other entries in the same group still flow through.
    """
    seconds_by_key: dict[tuple[str, str, date], int] = defaultdict(int)
    descriptions_by_key: dict[tuple[str, str, date], set[str]] = defaultdict(set)
    stats = {"skipped_running": 0, "no_project_cells": 0}

    for group in entries:
        # project_id=null means "tracked with no project". We keep these
        # — they're still real hours that need to land in the time-bank.
        project_id_raw = group.get("project_id")
        project_id = str(project_id_raw) if project_id_raw is not None else ""

        user_id = str(group.get("user_id", ""))
        if not user_id:
            continue

        # Description sits on the group, NOT on individual time entries.
        # Reports v3 groups by (user, project, description), so each group's
        # description applies to every nested session.
        group_description = (group.get("description") or "").strip()

        for entry in group.get("time_entries") or []:
            if entry.get("stop") is None:
                stats["skipped_running"] += 1
                continue
            seconds = entry.get("seconds")
            if not isinstance(seconds, (int, float)) or seconds <= 0:
                continue

            start_raw = entry.get("start") or ""
            try:
                start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            # If Toggl omits the tz (unlikely on Reports v3 but defensive),
            # assume UTC — that's what their docs claim everywhere.
            if start_dt.tzinfo is None:
                start_dt = start_dt.replace(tzinfo=timezone.utc)
            oslo_date = start_dt.astimezone(_OSLO).date()

            key = (user_id, project_id, oslo_date)
            seconds_by_key[key] += int(seconds)

            if group_description:
                descriptions_by_key[key].add(group_description)

    # Track no-project cells for the result summary.
    stats["no_project_cells"] = sum(
        1 for key in seconds_by_key if key[1] == ""
    )

    return seconds_by_key, descriptions_by_key, stats


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
    toggl_project_names: dict[str, str],
    descriptions: dict[tuple[str, str, date], set[str]],
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
        cell_descriptions = _join_descriptions(descriptions.get(key, set()))
        toggl_project_label = _resolve_toggl_project_label(
            key[1], toggl_project_names
        )
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
                    description=cell_descriptions,
                    toggl_project_label=toggl_project_label,
                    user_uuid_lookup=user_uuid_lookup,
                    user_name_lookup=user_name_lookup,
                    project_map=project_map,
                )
                result.rows_created += 1
            else:
                # Check whether any of the four "writable" fields drifted:
                # hours, description, toggl_project_name, or the project
                # relation (the latter when a previously-unmatched project
                # has since been mirrored).
                current_hours = _read_number_prop(row, TIMER_PROPS["hours"]) or 0.0
                current_description = _read_rich_text(
                    row.get("properties") or {}, TIMER_PROPS["description"]
                )
                current_project_name = _read_rich_text(
                    row.get("properties") or {}, TIMER_PROPS["toggl_project_name"]
                )
                hours_changed = abs(current_hours - hours) > _HOURS_EPSILON
                description_changed = current_description != cell_descriptions
                project_name_changed = current_project_name != toggl_project_label
                notion_project_page = (
                    project_map.get(key[1]) if key[1] else None
                )
                current_relation_ids = _read_relation_ids(
                    row.get("properties") or {}, TIMER_PROPS["project"]
                )
                relation_changed = (
                    [notion_project_page] if notion_project_page else []
                ) != current_relation_ids

                if (
                    hours_changed
                    or description_changed
                    or project_name_changed
                    or relation_changed
                ):
                    await _patch_timer_row(
                        row["id"],
                        hours=hours,
                        description=cell_descriptions,
                        toggl_project_label=toggl_project_label,
                        notion_project_page=notion_project_page,
                    )
                    result.rows_updated += 1
        except Exception as err:
            logger.exception(
                "toggl hours: cell %s failed", key
            )
            result.errors.append(f"cell {key}: {err}")


def _resolve_toggl_project_label(
    project_id: str, toggl_project_names: dict[str, str]
) -> str:
    """Human-readable Toggl project name for the row's Toggl Prosjekt navn column.

    - Empty project_id (no Toggl project on the entry) → "Uten prosjekt"
    - project_id resolvable → its Toggl name
    - project_id present but unknown (Toggl API failed or stale) → the id itself
      as a fallback so the row still carries something attributable
    """
    if not project_id:
        return "Uten prosjekt"
    return toggl_project_names.get(project_id) or project_id


def _join_descriptions(descs: set[str]) -> str:
    """Join unique descriptions with '; ', truncate to fit Notion's rich_text limit.

    Notion's rich_text caps a single text segment at 2000 chars; we leave
    headroom for safety and an ellipsis tail when truncated.
    """
    if not descs:
        return ""
    joined = "; ".join(sorted(descs))
    if len(joined) > 1900:
        return joined[:1897] + "..."
    return joined


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

    Returns None if user_id or date is missing/malformed. project_id can
    be empty string — that marks a "no-project" Toggl entry, which is a
    valid distinct cell from a row with a project.
    """
    props = row.get("properties") or {}
    user_id = _read_rich_text(props, TIMER_PROPS["toggl_user_id"])
    project_id = _read_rich_text(props, TIMER_PROPS["toggl_project_id"])
    date_str = _read_date_prop(props, TIMER_PROPS["date"])
    if not user_id or not date_str:
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


def _read_relation_ids(props: dict[str, Any], name: str) -> list[str]:
    prop = props.get(name) or {}
    items = prop.get("relation") or []
    return [i["id"] for i in items if "id" in i]


async def _create_timer_row(
    *,
    db_id: str,
    key: tuple[str, str, date],
    hours: float,
    description: str,
    toggl_project_label: str,
    user_uuid_lookup: dict[str, str],
    user_name_lookup: dict[str, str],
    project_map: dict[str, str],
) -> None:
    """Create one Timer row.

    Caller has already verified the user is resolvable. The project
    relation is set only when project_map has a Notion page for this
    Toggl project_id — otherwise the relation property is omitted so
    the row lands with an empty relation cell (which Notion accepts).
    """
    user_id, project_id, dato = key
    notion_user_uuid = user_uuid_lookup[user_id]
    user_name = user_name_lookup.get(user_id, "")
    notion_project_page = project_map.get(project_id) if project_id else None
    title = f"{user_name or user_id} — {dato.isoformat()}"

    properties: dict[str, Any] = {
        TIMER_PROPS["name"]: {
            "title": [{"text": {"content": title}}]
        },
        TIMER_PROPS["date"]: {"date": {"start": dato.isoformat()}},
        TIMER_PROPS["employee"]: {
            "people": [{"object": "user", "id": notion_user_uuid}]
        },
        TIMER_PROPS["hours"]: {"number": hours},
        TIMER_PROPS["description"]: {
            "rich_text": [{"text": {"content": description}}] if description else []
        },
        TIMER_PROPS["toggl_project_name"]: {
            "rich_text": [{"text": {"content": toggl_project_label}}]
        },
        TIMER_PROPS["toggl_user_id"]: {
            "rich_text": [{"text": {"content": user_id}}]
        },
        TIMER_PROPS["toggl_project_id"]: {
            "rich_text": [{"text": {"content": project_id}}] if project_id else []
        },
    }
    if notion_project_page:
        properties[TIMER_PROPS["project"]] = {
            "relation": [{"id": notion_project_page}]
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


async def _patch_timer_row(
    page_id: str,
    *,
    hours: float,
    description: str,
    toggl_project_label: str,
    notion_project_page: str | None,
) -> None:
    """Patch the writable fields of an existing Timer row.

    Always updates hours, description, and Toggl Prosjekt navn (these can
    all drift on retroactive edits). The project relation is set when
    `notion_project_page` is provided; otherwise it's explicitly cleared
    (an unmatched project that gets mirrored later will populate; one
    that gets removed clears).
    """
    properties: dict[str, Any] = {
        TIMER_PROPS["hours"]: {"number": hours},
        TIMER_PROPS["description"]: {
            "rich_text": [{"text": {"content": description}}] if description else []
        },
        TIMER_PROPS["toggl_project_name"]: {
            "rich_text": [{"text": {"content": toggl_project_label}}]
        },
        TIMER_PROPS["project"]: {
            "relation": [{"id": notion_project_page}] if notion_project_page else []
        },
    }
    async with _notion_client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{page_id}",
                json={"properties": properties},
            ),
            op_name=f"PATCH /pages/{page_id} timer-row",
        )
        _raise_for_status(response)
