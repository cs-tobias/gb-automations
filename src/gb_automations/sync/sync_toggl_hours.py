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
  5. Pre-resolve each aggregated user_id to a notion_user_uuid where
     possible. Cells whose user can't be matched are NOT skipped —
     they're counted under `unmatched_user_kept` and still written with
     an empty Ansatt property + Toggl Bruker navn populated.
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
    # CSV rows with zero/unparseable Duration. Toggl's CSV export omits
    # running entries entirely (no stop time → no Duration), so this is
    # only ever non-zero for genuinely malformed rows. Effectively
    # replaces the old "skipped_running" counter.
    skipped_zero_duration: int = 0
    # Informational: cells where the Toggl user's email doesn't match any
    # active Notion workspace member (ex-employees, freelancers without
    # Notion access). NOT skipped — the row still lands with empty `Ansatt`
    # and `Toggl Bruker navn` populated, so the hours appear in the
    # time-bank but aren't attributed to a clickable Notion person.
    unmatched_user_kept: int = 0
    # Informational: cells where the Toggl project_id has no Notion
    # relation mapped. NOT skipped — they still land in Notion with an
    # empty relation and Toggl Prosjekt navn populated.
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
        csv_rows = await _fetch_entries_multi_year(
            settings.toggl_workspace_id, window_start, window_end
        )
    except Exception as err:
        logger.exception("toggl hours: Reports v3 CSV fetch failed")
        result.action = "failed"
        result.note = f"Reports v3 CSV fetch failed: {err}"
        return result
    result.entries_fetched = len(csv_rows)
    logger.info(
        "toggl hours: fetched %d CSV rows for window %s … %s",
        len(csv_rows),
        result.window_start,
        result.window_end,
    )

    # Build the lookups we need to resolve CSV's display labels back to
    # our canonical ids. Both fall back to "no match" rather than
    # dropping rows — every CSV row lands as a Notion row regardless.
    toggl_user_emails = await _load_toggl_user_emails()
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

    # CSV lookup tables:
    #   user_id_by_email: lowercased email → toggl_user_id (str)
    #   project_id_by_name: casefolded project name → toggl_project_id (str)
    #   user_name_by_email: lowercased email → display name fallback
    user_id_by_email: dict[str, str] = {}
    user_name_by_email: dict[str, str] = {}
    for uid, info in toggl_user_emails.items():
        em = (info.get("email") or "").lower()
        nm = info.get("name") or ""
        if em:
            user_id_by_email[em] = uid
            user_name_by_email[em] = nm

    # toggl_project_id → display name, for the Toggl Prosjekt navn column.
    # Built from list_projects so we also have id→name (CSV gives us name,
    # but if a row's project name doesn't match the cache we keep CSV's
    # raw name as the label fallback).
    project_id_by_name: dict[str, str] = {}
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
                project_id_by_name[name.casefold()] = str(pid)
    except Exception as err:
        logger.warning(
            "toggl hours: list_projects failed (%s) — CSV rows with "
            "matching project names won't resolve to ids, but will "
            "still land in Notion with the CSV name as Toggl Prosjekt navn",
            err,
        )

    # Aggregate CSV rows into (toggl_user_id, toggl_project_id, oslo_day)
    # cells. Returns descriptions, entry counts, and row metadata too —
    # all consumed by the reconcile path below.
    (
        aggregate,
        descriptions,
        entry_counts,
        row_meta,
        agg_stats,
    ) = _aggregate(
        csv_rows,
        user_id_by_email=user_id_by_email,
        project_id_by_name=project_id_by_name,
        user_name_by_email=user_name_by_email,
    )
    result.cells_aggregated = len(aggregate)
    result.skipped_zero_duration = agg_stats.get("skipped_zero_duration", 0)
    result.no_project_kept = agg_stats.get("no_project_cells", 0)

    # Build user_uuid_lookup / user_name_lookup for the cell-loop and
    # writers. The aggregate's user_key slot is either a real
    # toggl_user_id (when CSV's email matched our cache) or the email
    # itself (when it didn't). Either way the writers do the same thing
    # downstream — look up by user_key, fall back to CSV's stored
    # row_meta name when not resolvable.
    user_uuid_lookup: dict[str, str] = {}
    user_name_lookup: dict[str, str] = {}
    for key in aggregate:
        user_key = key[0]
        # First try the canonical id path (toggl_user_id → email →
        # Notion uuid). If user_key isn't a toggl_user_id we have in
        # cache, try interpreting it as the email directly (the
        # fallback _aggregate uses).
        info = toggl_user_emails.get(user_key) or {}
        email = info.get("email") or ""
        if not email and "@" in user_key:
            email = user_key
        name = info.get("name") or row_meta.get(key, {}).get("user_name", "")
        user_name_lookup[user_key] = name
        notion_uuid = notion_user_by_email.get(email.lower()) if email else None
        if notion_uuid:
            user_uuid_lookup[user_key] = notion_uuid

    # Group cells by year so each Timer YYYY DB is reconciled
    # independently. EVERY cell flows through — no gate.
    cells_by_year: dict[int, dict[tuple[str, str, date], int]] = defaultdict(dict)
    for key, seconds in aggregate.items():
        user_key, project_key, oslo_date = key
        if user_key not in user_uuid_lookup:
            result.unmatched_user_kept += 1
        if project_key and project_key not in known_project_map:
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
                entry_counts=entry_counts,
                row_meta=row_meta,
                result=result,
            )
        except Exception as err:
            logger.exception(
                "toggl hours: year %d reconcile crashed", year
            )
            result.errors.append(f"year {year} reconcile failed: {err}")

    logger.info(
        "toggl hours sync done: created=%d updated=%d archived=%d "
        "(skipped_zero_duration=%d unmatched_user_kept=%d "
        "unmatched_project_kept=%d no_project_kept=%d) errors=%d",
        result.rows_created,
        result.rows_updated,
        result.rows_archived,
        result.skipped_zero_duration,
        result.unmatched_user_kept,
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
) -> list[dict[str, str]]:
    """Fetch Toggl Reports v3 CSV-export rows across an arbitrary window.

    Reports v3 caps each call at a 1-year range. For multi-year backfills
    we slice the window into per-calendar-year chunks and concatenate.

    Returns flat CSV rows (`list[dict[str, str]]`, one per Toggl session).
    The CSV-export endpoint is used instead of the JSON variant because
    JSON silently truncates at a hidden cap — see notes in
    `toggl_client.export_time_entries_csv`.
    """
    if window_start.year == window_end.year:
        return await toggl_client.export_time_entries_csv(
            workspace_id,
            start_date=window_start.isoformat(),
            end_date=window_end.isoformat(),
        )

    all_rows: list[dict[str, str]] = []
    for year in range(window_start.year, window_end.year + 1):
        slice_start = max(window_start, date(year, 1, 1))
        slice_end = min(window_end, date(year, 12, 31))
        logger.info(
            "toggl hours: multi-year fetch %s … %s",
            slice_start.isoformat(),
            slice_end.isoformat(),
        )
        chunk = await toggl_client.export_time_entries_csv(
            workspace_id,
            start_date=slice_start.isoformat(),
            end_date=slice_end.isoformat(),
        )
        all_rows.extend(chunk)
    return all_rows


# ============================================================
# Reconciliation — verify Notion matches Toggl after a backfill
# ============================================================


# Hours-diff band below which we treat the two sides as "matching." Notion
# stores hours rounded to 2 decimals; Toggl stores seconds. Across thousands
# of rows the rounding accumulates to ~tenths of an hour. 1.0h is a generous
# floor that catches real data loss without flagging rounding drift.
_VERIFY_HOURS_TOLERANCE = 1.0


async def reconcile_toggl_notion_hours(
    *,
    window_start: date,
    window_end: date,
) -> dict[str, Any]:
    """Compare Toggl's truth against the Notion Timer DBs for a window.

    Read-only on both sides. Toggl side uses the CSV export endpoint
    (the JSON variant of the same path silently truncates and must not
    be used for payroll-grade reconciliation — see
    `toggl_client.export_time_entries_csv` for the gory details).

    Returns three Toggl numbers so the aggregation behavior is explicit:
      - total_hours              (sum of every CSV row's Duration)
      - raw_entry_count          (count of CSV data rows = real sessions)
      - expected_aggregated_cells (unique (user_key, project_key, oslo_day)
                                   tuples — what we EXPECT Notion to hold)

    Plus Notion totals (hours, rows, with/without relation, with/without
    Ansatt, sum of Antall økter, by-year), and a diff block flagging
    out-of-tolerance discrepancies on three independent axes:
    hours, row count, and Antall økter total.
    """
    if not settings.toggl_workspace_id:
        return {"action": "skipped", "note": "TOGGL_WORKSPACE_ID not set"}

    # --- Toggl side ---
    csv_rows = await _fetch_entries_multi_year(
        settings.toggl_workspace_id, window_start, window_end
    )

    # Build the same lookups the engine uses, so expected_aggregated_cells
    # is computed against the SAME key shape the writer uses. If a CSV
    # row's email matches the cache we use toggl_user_id; otherwise the
    # email itself. Same idea for project name → toggl_project_id.
    toggl_user_emails = await _load_toggl_user_emails()
    user_id_by_email: dict[str, str] = {}
    for uid, info in toggl_user_emails.items():
        em = (info.get("email") or "").lower()
        if em:
            user_id_by_email[em] = uid
    project_id_by_name: dict[str, str] = {}
    try:
        for p in await toggl_client.list_projects(settings.toggl_workspace_id):
            pid = p.get("id")
            name = p.get("name") or ""
            if pid is not None and name:
                project_id_by_name[name.casefold()] = str(pid)
    except Exception as err:
        logger.warning(
            "verify: list_projects failed (%s) — expected_aggregated_cells "
            "will use synthetic project keys for cache misses",
            err,
        )

    toggl_seconds = 0
    raw_entry_count = 0
    expected_cells: set[tuple[str, str, date]] = set()
    for row in csv_rows:
        seconds = _parse_csv_duration_to_seconds(
            (row.get("Duration") or "").strip()
        )
        if seconds <= 0:
            continue
        raw_entry_count += 1
        toggl_seconds += seconds

        email = (row.get("Email") or "").strip().lower()
        user_name = (row.get("User") or "").strip()
        project_name = (row.get("Project") or "").strip()
        start_date_str = (row.get("Start date") or "").strip()
        try:
            oslo_date = date.fromisoformat(start_date_str)
        except ValueError:
            continue

        user_key = (
            user_id_by_email.get(email)
            or email
            or (user_name.casefold() if user_name else "_anonymous_")
        )
        project_key = (
            project_id_by_name.get(project_name.casefold())
            if project_name
            else ""
        )
        if project_name and not project_key:
            project_key = f"_name:{project_name.casefold()}"

        expected_cells.add((user_key, project_key, oslo_date))

    toggl_total_hours = round(toggl_seconds / 3600.0, 2)

    # --- Notion side ---
    notion_total_hours = 0.0
    notion_total_rows = 0
    notion_total_entry_count = 0
    rows_with_relation = 0
    rows_without_relation = 0
    rows_with_ansatt = 0
    rows_without_ansatt = 0
    by_year: dict[str, dict[str, float | int]] = {}

    for year in range(window_start.year, window_end.year + 1):
        try:
            db_id = await notion_timer_db.get_timer_db_for_year(year)
        except Exception as err:
            logger.warning(
                "verify: could not resolve Timer DB for year %d: %s", year, err
            )
            continue
        year_start = max(window_start, date(year, 1, 1))
        year_end = min(window_end, date(year, 12, 31))
        rows = await _query_timer_rows_in_window(db_id, year_start, year_end)

        year_hours = 0.0
        year_rows = 0
        year_entry_count = 0
        for row in rows:
            props = row.get("properties") or {}
            hours = _read_number_prop(row, TIMER_PROPS["hours"]) or 0.0
            entry_count = (
                _read_number_prop(row, TIMER_PROPS["entry_count"]) or 0
            )
            year_hours += hours
            year_entry_count += int(entry_count)
            year_rows += 1
            relation_ids = _read_relation_ids(props, TIMER_PROPS["project"])
            if relation_ids:
                rows_with_relation += 1
            else:
                rows_without_relation += 1
            people_ids = _read_people_ids(props, TIMER_PROPS["employee"])
            if people_ids:
                rows_with_ansatt += 1
            else:
                rows_without_ansatt += 1
        notion_total_hours += year_hours
        notion_total_rows += year_rows
        notion_total_entry_count += year_entry_count
        by_year[str(year)] = {
            "hours": round(year_hours, 2),
            "rows": year_rows,
            "entry_count": year_entry_count,
        }

    notion_total_hours = round(notion_total_hours, 2)

    diff_hours = round(toggl_total_hours - notion_total_hours, 2)
    diff_entry_count = raw_entry_count - notion_total_entry_count
    return {
        "window": {
            "from": window_start.isoformat(),
            "to": window_end.isoformat(),
        },
        "toggl": {
            "total_hours": toggl_total_hours,
            "raw_entry_count": raw_entry_count,
            "expected_aggregated_cells": len(expected_cells),
        },
        "notion": {
            "total_hours": notion_total_hours,
            "total_rows": notion_total_rows,
            "total_entry_count": notion_total_entry_count,
            "rows_with_relation": rows_with_relation,
            "rows_without_relation": rows_without_relation,
            "rows_with_ansatt": rows_with_ansatt,
            "rows_without_ansatt": rows_without_ansatt,
            "by_year": by_year,
        },
        "diff": {
            "hours": diff_hours,
            "hours_within_tolerance": abs(diff_hours) <= _VERIFY_HOURS_TOLERANCE,
            "row_count_match": notion_total_rows == len(expected_cells),
            "entry_count_diff": diff_entry_count,
            "entry_count_match": diff_entry_count == 0,
        },
    }


# ============================================================
# Aggregation
# ============================================================


def _parse_csv_duration_to_seconds(duration: str) -> int:
    """Parse a Toggl CSV `Duration` cell (`HH:MM:SS`) to total seconds.

    Returns 0 on unparseable input — caller decides whether to skip.
    """
    try:
        parts = duration.split(":")
        if len(parts) != 3:
            return 0
        h, m, s = (int(p) for p in parts)
        if h < 0 or m < 0 or s < 0:
            return 0
        return h * 3600 + m * 60 + s
    except (ValueError, AttributeError):
        return 0


def _csv_user_label(email: str, name: str) -> str:
    """Stable label for a Toggl user when matching against the user cache.

    The CSV gives us `Email` AND `User` (display name). We key the
    aggregation tuple on a normalized email when present (so a user who
    appears in cache gets their proper toggl_user_id substituted later),
    and fall back to the display name when the CSV row has no email
    (rare — old/anonymized entries).
    """
    if email:
        return email.strip().lower()
    return (name or "").strip()


def _aggregate(
    csv_rows: list[dict[str, str]],
    *,
    user_id_by_email: dict[str, str],
    project_id_by_name: dict[str, str],
    user_name_by_email: dict[str, str],
) -> tuple[
    dict[tuple[str, str, date], int],
    dict[tuple[str, str, date], set[str]],
    dict[tuple[str, str, date], int],
    dict[str, dict[str, str]],
    dict[str, int],
]:
    """Aggregate flat Toggl CSV rows to (toggl_user_id, toggl_project_id, oslo_day) cells.

    Returns:
        - seconds_by_key: cell → total seconds
        - descriptions_by_key: cell → set of unique non-empty descriptions
        - entry_count_by_key: cell → count of raw CSV rows merged in
          (the `Antall økter` column — independent verification metric)
        - row_meta_by_key: cell → {"user_name": str, "project_name": str}
          (CSV's display labels — used to populate `Toggl Bruker navn` /
          `Toggl Prosjekt navn` columns. Last-write-wins per cell, which
          is fine because the same (email, project name, day) should
          carry the same labels for every CSV row.)
        - stats: per-run counters

    NEVER drops a row:
      - Unmatched email → cell's user_id slot becomes the email itself
        (still unique per user). Notion row will have empty Ansatt.
      - Unmatched project name (or blank) → cell's project_id slot is
        empty string. Notion row will have empty Prosjekt relation.
      - Zero-duration / malformed row → still counted under
        skipped_zero_duration but NOT folded into a cell (it has no
        hours to contribute). This matches Toggl's own behavior — a
        zero-duration session shouldn't increment the entry count of
        a cell that has real hours.

    The CSV's `Start date` is the user's workspace-local calendar day
    (Goldbox is Europe/Oslo) and is used directly — no UTC conversion
    needed, since Toggl already bucketed the date for us in the export.
    """
    seconds_by_key: dict[tuple[str, str, date], int] = defaultdict(int)
    descriptions_by_key: dict[tuple[str, str, date], set[str]] = defaultdict(set)
    entry_count_by_key: dict[tuple[str, str, date], int] = defaultdict(int)
    row_meta_by_key: dict[tuple[str, str, date], dict[str, str]] = {}
    stats = {"skipped_zero_duration": 0, "no_project_cells": 0}

    for row in csv_rows:
        duration = (row.get("Duration") or "").strip()
        seconds = _parse_csv_duration_to_seconds(duration)
        if seconds <= 0:
            stats["skipped_zero_duration"] += 1
            continue

        email = (row.get("Email") or "").strip().lower()
        user_name = (row.get("User") or "").strip()
        project_name = (row.get("Project") or "").strip()
        description = (row.get("Description") or "").strip()
        start_date_str = (row.get("Start date") or "").strip()

        try:
            oslo_date = date.fromisoformat(start_date_str)
        except ValueError:
            # Malformed `Start date` column — extremely rare, but if it
            # happens we'd rather flag it than silently lose hours.
            logger.warning(
                "toggl csv: unparseable Start date %r — skipping row %r",
                start_date_str, row,
            )
            stats["skipped_zero_duration"] += 1
            continue

        # Resolve to the canonical toggl_user_id when the email is in
        # our user cache; otherwise use the email itself as the key
        # (still uniquely identifies that user across CSV rows).
        toggl_user_id = user_id_by_email.get(email) if email else None
        user_key = toggl_user_id or email or (user_name.casefold() if user_name else "")
        if not user_key:
            # Truly anonymous row (no email, no name) — treat as a single
            # bucket so we don't lose hours. Should never happen in
            # practice but defensive.
            user_key = "_anonymous_"

        # Resolve project name to toggl_project_id when cached; else
        # empty string (= "Uten prosjekt" in display layer).
        project_key = project_id_by_name.get(project_name.casefold()) if project_name else ""
        if not project_key and project_name:
            # Project name present but not in our cache — fall back to
            # the casefolded name as the dedup key. Two rows for the
            # same not-yet-cached project on the same day will still
            # aggregate cleanly together.
            project_key = f"_name:{project_name.casefold()}"

        key = (user_key, project_key, oslo_date)
        seconds_by_key[key] += seconds
        entry_count_by_key[key] += 1
        if description:
            descriptions_by_key[key].add(description)

        if key not in row_meta_by_key:
            # First-seen wins for display labels — fine since the labels
            # are stable per (user, project) and the CSV row's columns
            # carry the same name/project for every entry in the cell.
            row_meta_by_key[key] = {
                "user_name": user_name or user_name_by_email.get(email, ""),
                "project_name": project_name,
            }

    stats["no_project_cells"] = sum(
        1 for k in seconds_by_key if k[1] == ""
    )

    return (
        seconds_by_key,
        descriptions_by_key,
        entry_count_by_key,
        row_meta_by_key,
        stats,
    )


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
    entry_counts: dict[tuple[str, str, date], int],
    row_meta: dict[tuple[str, str, date], dict[str, str]],
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
        cell_entry_count = entry_counts.get(key, 0)
        cell_meta = row_meta.get(key, {})
        toggl_project_label = _resolve_toggl_project_label(
            key[1], toggl_project_names, cell_meta.get("project_name", "")
        )
        try:
            if seconds <= 0 and row is not None:
                await archive_page(row["id"])
                result.rows_archived += 1
                continue
            if seconds <= 0:
                continue
            hours = round(seconds / 3600.0, 2)
            # Resolve the user label that should land on the row. Prefer
            # the cache's display name; fall back to whatever the CSV
            # reported in row_meta; final fallback is a stable synthetic
            # so the row title isn't blank.
            row_user_name = (
                user_name_lookup.get(key[0])
                or cell_meta.get("user_name", "")
            )
            user_label = row_user_name or f"toggl_user_{key[0]}"
            notion_user_uuid = user_uuid_lookup.get(key[0])
            notion_project_page = (
                project_map.get(key[1]) if key[1] else None
            )

            if row is None:
                await _create_timer_row(
                    db_id=db_id,
                    key=key,
                    hours=hours,
                    entry_count=cell_entry_count,
                    description=cell_descriptions,
                    toggl_project_label=toggl_project_label,
                    toggl_user_label=user_label,
                    notion_project_page=notion_project_page,
                    notion_user_uuid=notion_user_uuid,
                )
                result.rows_created += 1
            else:
                # Drift detection across all writable fields. Any of
                # these can change after the row was first written
                # (retroactive Toggl edits adding/removing sessions,
                # new TogglProject mapping, employee re-added to Notion).
                row_props = row.get("properties") or {}
                current_hours = _read_number_prop(row, TIMER_PROPS["hours"]) or 0.0
                current_entry_count = (
                    _read_number_prop(row, TIMER_PROPS["entry_count"]) or 0
                )
                current_description = _read_rich_text(
                    row_props, TIMER_PROPS["description"]
                )
                current_project_name = _read_rich_text(
                    row_props, TIMER_PROPS["toggl_project_name"]
                )
                current_user_name = _read_rich_text(
                    row_props, TIMER_PROPS["toggl_user_name"]
                )
                hours_changed = abs(current_hours - hours) > _HOURS_EPSILON
                entry_count_changed = int(current_entry_count) != cell_entry_count
                description_changed = current_description != cell_descriptions
                project_name_changed = current_project_name != toggl_project_label
                user_name_changed = current_user_name != user_label
                current_relation_ids = _read_relation_ids(
                    row_props, TIMER_PROPS["project"]
                )
                relation_changed = (
                    [notion_project_page] if notion_project_page else []
                ) != current_relation_ids
                current_people_ids = _read_people_ids(
                    row_props, TIMER_PROPS["employee"]
                )
                people_changed = (
                    [notion_user_uuid] if notion_user_uuid else []
                ) != current_people_ids

                if (
                    hours_changed
                    or entry_count_changed
                    or description_changed
                    or project_name_changed
                    or user_name_changed
                    or relation_changed
                    or people_changed
                ):
                    await _patch_timer_row(
                        row["id"],
                        hours=hours,
                        entry_count=cell_entry_count,
                        description=cell_descriptions,
                        toggl_project_label=toggl_project_label,
                        toggl_user_label=user_label,
                        notion_project_page=notion_project_page,
                        notion_user_uuid=notion_user_uuid,
                    )
                    result.rows_updated += 1
        except Exception as err:
            logger.exception(
                "toggl hours: cell %s failed", key
            )
            result.errors.append(f"cell {key}: {err}")


def _resolve_toggl_project_label(
    project_key: str,
    toggl_project_names: dict[str, str],
    csv_project_name: str = "",
) -> str:
    """Human-readable Toggl project name for the row's Toggl Prosjekt navn column.

    Resolution order:
      1. Empty project_key (no Toggl project on the entry) → "Uten prosjekt"
      2. project_key looks like our synthetic "_name:<x>" sentinel
         (CSV name didn't match the cache) → use the original CSV name
         from row_meta when available, else strip the prefix.
      3. project_key in `toggl_project_names` (a real toggl_project_id) →
         that lookup's display name.
      4. Final fallback: the CSV's project name from row_meta, or the
         project_key itself.
    """
    if not project_key:
        return "Uten prosjekt"
    if project_key.startswith("_name:"):
        return csv_project_name or project_key[len("_name:"):]
    return (
        toggl_project_names.get(project_key)
        or csv_project_name
        or project_key
    )


def _join_descriptions(descs: set[str]) -> str:
    """Join unique descriptions with ', ', truncate to fit Notion's rich_text limit.

    Notion's rich_text caps a single text segment at 2000 chars; we leave
    headroom for safety and an ellipsis tail when truncated.
    """
    if not descs:
        return ""
    joined = ", ".join(sorted(descs))
    if len(joined) > 1900:
        return joined[:1897] + "..."
    return joined


# ============================================================
# Notion read / write helpers
# ============================================================


async def _query_timer_rows_in_window(
    db_id: str, window_start: date, window_end: date
) -> list[dict[str, Any]]:
    """Pull every non-archived Timer row whose Dato is in [start, end].

    Notion's date filter SILENTLY IGNORES a filter object with both
    `on_or_after` and `on_or_before` keys in the same dict — it returns
    every row in the DB as if there were no filter at all. The fix is to
    wrap them in an explicit `and` array with one clause per bound, which
    Notion's query engine actually honors.
    """
    rows: list[dict[str, Any]] = []
    start_cursor: str | None = None
    body_base: dict[str, Any] = {
        "filter": {
            "and": [
                {
                    "property": TIMER_PROPS["date"],
                    "date": {"on_or_after": window_start.isoformat()},
                },
                {
                    "property": TIMER_PROPS["date"],
                    "date": {"on_or_before": window_end.isoformat()},
                },
            ],
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


def _read_people_ids(props: dict[str, Any], name: str) -> list[str]:
    prop = props.get(name) or {}
    items = prop.get("people") or []
    return [i["id"] for i in items if "id" in i]


async def _create_timer_row(
    *,
    db_id: str,
    key: tuple[str, str, date],
    hours: float,
    entry_count: int,
    description: str,
    toggl_project_label: str,
    toggl_user_label: str,
    notion_project_page: str | None,
    notion_user_uuid: str | None,
) -> None:
    """Create one Timer row.

    All resolution work — user→Notion uuid, project name→relation page —
    happens in `_reconcile_year` before calling here. This function is
    the dumb writer: assemble properties + POST.

    `key[0]` (user slot) is either a real toggl_user_id (if CSV's email
    matched the user cache) or the email itself (fallback). Either way
    it's a stable per-user dedup key — `_row_key` will read it back the
    same way on the next sync. Same shape for `key[1]` (project slot):
    real id, empty string (no project), or `_name:<x>` sentinel for
    unmatched project names.
    """
    user_key, project_key, dato = key
    title = f"{toggl_user_label} — {dato.isoformat()}"

    properties: dict[str, Any] = {
        TIMER_PROPS["name"]: {
            "title": [{"text": {"content": title}}]
        },
        TIMER_PROPS["date"]: {"date": {"start": dato.isoformat()}},
        TIMER_PROPS["hours"]: {"number": hours},
        TIMER_PROPS["entry_count"]: {"number": entry_count},
        TIMER_PROPS["description"]: {
            "rich_text": [{"text": {"content": description}}] if description else []
        },
        TIMER_PROPS["toggl_project_name"]: {
            "rich_text": [{"text": {"content": toggl_project_label}}]
        },
        TIMER_PROPS["toggl_user_name"]: {
            "rich_text": [{"text": {"content": toggl_user_label}}]
        },
        TIMER_PROPS["toggl_user_id"]: {
            "rich_text": [{"text": {"content": user_key}}]
        },
        TIMER_PROPS["toggl_project_id"]: {
            "rich_text": [{"text": {"content": project_key}}] if project_key else []
        },
    }
    if notion_user_uuid:
        properties[TIMER_PROPS["employee"]] = {
            "people": [{"object": "user", "id": notion_user_uuid}]
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
            op_name=f"POST /pages timer({user_key},{project_key},{dato})",
        )
        _raise_for_status(response)


async def _patch_timer_row(
    page_id: str,
    *,
    hours: float,
    entry_count: int,
    description: str,
    toggl_project_label: str,
    toggl_user_label: str,
    notion_project_page: str | None,
    notion_user_uuid: str | None,
) -> None:
    """Patch the writable fields of an existing Timer row.

    Always rewrites hours, entry_count (`Antall økter`), description,
    Toggl Prosjekt navn, and Toggl Bruker navn — all can drift on
    retroactive edits. The Prosjekt relation and Ansatt people-property
    are set when their respective Notion match exists; otherwise both
    are explicitly cleared (a previously-matched row that later becomes
    unmatched reflects the new state).
    """
    properties: dict[str, Any] = {
        TIMER_PROPS["hours"]: {"number": hours},
        TIMER_PROPS["entry_count"]: {"number": entry_count},
        TIMER_PROPS["description"]: {
            "rich_text": [{"text": {"content": description}}] if description else []
        },
        TIMER_PROPS["toggl_project_name"]: {
            "rich_text": [{"text": {"content": toggl_project_label}}]
        },
        TIMER_PROPS["toggl_user_name"]: {
            "rich_text": [{"text": {"content": toggl_user_label}}]
        },
        TIMER_PROPS["project"]: {
            "relation": [{"id": notion_project_page}] if notion_project_page else []
        },
        TIMER_PROPS["employee"]: {
            "people": [{"object": "user", "id": notion_user_uuid}] if notion_user_uuid else []
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
