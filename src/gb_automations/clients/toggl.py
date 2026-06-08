"""Async Toggl Track v9 + Reports v3 REST client.

Mirrors the shape of `clients/frame.py` — same httpx + retry pattern, same
event-hook logging, same `_raise_for_status` ergonomics. Auth is HTTP Basic
with the literal string `api_token` as the password (per Toggl's docs); the
real secret is the username field.

Reference: https://engineering.toggl.com/docs/

Phase 1 starts with only what the bootstrap needs to prove the connection
works:
    - whoami()             — GET /api/v9/me  (smoke test + resolves workspace id)
    - list_workspace_users() — workspace members (for the Notion ↔ Toggl user map)
    - list_projects()      — sanity-check what's already in the workspace

Project create/update, time-entry pulling, and Reports v3 land in later
slices as we wire up real flows. Don't speculatively add methods.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


TOGGL_API_BASE = "https://api.track.toggl.com"
# Toggl's API is normally fast (<500ms) but Reports v3 with broad filters can
# take a few seconds. 30s matches the Notion/Frame budget and covers worst
# cases without masking real outages.
_HTTP_TIMEOUT = 30.0


class TogglAPIError(RuntimeError):
    """Toggl HTTP error with body attached for diagnosis."""

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        message = (
            f"Toggl {response.status_code} "
            f"{response.request.method} {response.url}: {body}"
        )
        super().__init__(message)
        self.status_code = response.status_code
        self.body = body

    def is_stale_object(self) -> bool:
        """True if the error looks like 'the thing you asked about is gone'.

        Used by sync engines to evict cached Toggl ids and re-create on next
        pass — mirrors `NotionAPIError.is_stale_object`. 404 is the main
        signal; 403 also shows up when a project was deleted out from under
        an integration that lost permission.
        """
        return self.status_code in (403, 404)


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise TogglAPIError(response)


async def _with_retries(
    operation: Callable[[], Awaitable[httpx.Response]],
    *,
    op_name: str,
    max_attempts: int = 3,
) -> httpx.Response:
    """Exponential backoff on transient httpx errors + 5xx + 429 + 402.

    Toggl uses HTTP 402 (not 429) for rate-limit-exceeded post Sep 2025 — see
    https://support.toggl.com/en/articles/11484112. Treat it as transient so
    a brief burst doesn't fail a sync; the backoff (1s then 4s) is well under
    Toggl's sliding-window reset.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await operation()
        except (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
        ) as err:
            last_err = err
            if attempt + 1 >= max_attempts:
                break
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "toggl %s attempt %d failed (%s); retrying in %.1fs",
                op_name,
                attempt + 1,
                type(err).__name__,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        if (
            response.status_code in (402, 429)
            or 500 <= response.status_code < 600
        ) and attempt + 1 < max_attempts:
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "toggl %s attempt %d got %d; retrying in %.1fs",
                op_name,
                attempt + 1,
                response.status_code,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        return response
    assert last_err is not None
    raise last_err


async def _log_request(request: httpx.Request) -> None:
    logger.debug("toggl → %s %s", request.method, request.url.path)


async def _log_response(response: httpx.Response) -> None:
    logger.debug(
        "toggl ← %d %s %s",
        response.status_code,
        response.request.method,
        response.request.url.path,
    )


def _basic_auth_header(api_token: str) -> str:
    """Build the `Authorization: Basic …` value Toggl expects.

    Per https://engineering.toggl.com/docs/authentication, the API token goes
    in the username field with the literal string `api_token` as the password
    — base64'd in the standard HTTP Basic format.
    """
    raw = f"{api_token}:api_token"
    return "Basic " + base64.b64encode(raw.encode("utf-8")).decode("ascii")


async def _client() -> httpx.AsyncClient:
    """Build an httpx client pre-loaded with auth + logging hooks.

    Reads the token from `settings.toggl_api_token` at call time (not module
    import), so the bootstrap script's late-bound settings update works.
    """
    from gb_automations.config import settings

    if not settings.toggl_api_token:
        raise RuntimeError(
            "TOGGL_API_TOKEN is not configured. Get it from "
            "https://track.toggl.com/profile (signed in as the admin) and "
            "paste into .env."
        )
    return httpx.AsyncClient(
        base_url=TOGGL_API_BASE,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": _basic_auth_header(settings.toggl_api_token),
            "Content-Type": "application/json",
        },
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )


# ============================================================
# Reads — used by bootstrap + (later) /debug/toggl
# ============================================================


async def whoami() -> dict[str, Any]:
    """`GET /api/v9/me` — returns the authenticated user + their workspaces.

    Smoke test for the auth chain: token valid, Basic auth header correct,
    Toggl API reachable. Response includes `default_workspace_id` and a
    `workspaces` list — both used by the bootstrap script to pick the
    target workspace.
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get("/api/v9/me?with_related_data=true"),
            op_name="whoami",
        )
        _raise_for_status(response)
        return response.json()


async def list_workspace_users(workspace_id: str) -> list[dict[str, Any]]:
    """`GET /api/v9/workspaces/{id}/users` — members of the workspace.

    Each entry includes `id` (toggl_user_id), `email`, `name`, and `admin`.
    The bootstrap script prints these so the operator can map each toggl_user_id
    to a `Kontaktpersoner` (Contacts) Notion page — that mapping is what lets
    the nightly hours sync write Notion rows attributed to the right person.
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get(f"/api/v9/workspaces/{workspace_id}/users"),
            op_name="list_workspace_users",
        )
        _raise_for_status(response)
        return response.json() or []


async def list_projects(workspace_id: str) -> list[dict[str, Any]]:
    """`GET /api/v9/workspaces/{id}/projects` — every project in the workspace.

    Pages through with `per_page=200`, sorted by id (stable across requests
    so pagination doesn't skip/duplicate). `active=both` includes archived.
    Dedupes by id and stops on the first empty page.
    """
    all_projects: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    page = 1
    async with await _client() as client:
        while True:
            response = await _with_retries(
                lambda p=page: client.get(
                    f"/api/v9/workspaces/{workspace_id}/projects",
                    params={
                        "active": "both",
                        "per_page": 200,
                        "page": p,
                        "sort_field": "id",
                        "sort_order": "ASC",
                    },
                ),
                op_name="list_projects",
            )
            _raise_for_status(response)
            data = response.json() or []
            projects = data if isinstance(data, list) else data.get("items", [])
            if not projects:
                break
            for p in projects:
                pid = p.get("id")
                if pid is None or pid in seen_ids:
                    continue
                seen_ids.add(pid)
                all_projects.append(p)
            page += 1
    return all_projects


# ============================================================
# Project CRUD — used by sync_toggl_project
# ============================================================


async def get_project(
    workspace_id: str, project_id: str
) -> dict[str, Any]:
    """`GET /api/v9/workspaces/{wid}/projects/{pid}` — single project read.

    Used by the engine's stale-cache self-heal: a 404 means the project was
    deleted in Toggl's UI and our cached `toggl_project_id` needs to be
    evicted (the engine then falls through to create-or-adopt).
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get(
                f"/api/v9/workspaces/{workspace_id}/projects/{project_id}"
            ),
            op_name="get_project",
        )
        _raise_for_status(response)
        return response.json()


async def create_project(
    workspace_id: str, name: str, *, active: bool = True
) -> dict[str, Any]:
    """`POST /api/v9/workspaces/{wid}/projects` — create a project.

    Only `name` is required; everything else has server defaults. We pass
    `active=true` explicitly so the project lands in the team's default
    timer dropdowns — Toggl doesn't show archived projects there. Color
    is left unset (Toggl auto-assigns).
    """
    body = {"name": name, "active": active}
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/api/v9/workspaces/{workspace_id}/projects", json=body
            ),
            op_name="create_project",
        )
        _raise_for_status(response)
        return response.json()


async def export_time_entries_csv(
    workspace_id: str,
    *,
    start_date: str,
    end_date: str,
) -> list[dict[str, str]]:
    """Reports v3 CSV export — every time entry in `[start_date, end_date]`.

    Uses the same endpoint Toggl's own UI calls when you click "Export →
    CSV" in the Detailed Report: `POST /reports/api/v3/workspace/{wid}/
    search/time_entries.csv`. Returns flat, untruncated rows — one per
    actual time session — with no grouping or deduplication.

    Why not the JSON variant of the same path? The JSON endpoint silently
    truncates results at a hidden cap (~650 rows per query, irrespective
    of pagination) — proven against Goldbox's workspace by comparing
    `Q1+Q2+Q3+Q4 == FullYear` for CSV (matches) vs JSON (~13% of CSV).
    Payroll-grade completeness requires CSV. The JSON variant is not safe
    here and must not be reintroduced.

    Returns: `list[dict[str, str]]` — one dict per CSV data row, keyed by
    the CSV column headers. Goldbox-observed columns:
        User, Email, Client, Project, Task, Description, Billable,
        Start date, Start time, End date, End time, Duration, Tags,
        Currency, Amount
    The timestamps come back in the workspace's timezone (Europe/Oslo for
    Goldbox), naive (no offset suffix). Duration is `HH:MM:SS`.

    Date range is ISO 8601 (`YYYY-MM-DD`). Toggl enforces a max range of
    one year per call; multi-year callers must slice externally.
    """
    import csv
    import io

    body = {"start_date": start_date, "end_date": end_date}
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/reports/api/v3/workspace/{workspace_id}/search/time_entries.csv",
                json=body,
            ),
            op_name="export_time_entries_csv",
        )
        _raise_for_status(response)
        # Toggl returns the entire CSV in one response body — no pagination
        # cursor for the .csv variant. The body is plain text.
        reader = csv.DictReader(io.StringIO(response.text))
        rows = [dict(row) for row in reader]
    return rows


async def update_project(
    workspace_id: str,
    project_id: str,
    *,
    name: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    """`PUT /api/v9/workspaces/{wid}/projects/{pid}` — partial update.

    Used today only for renames (Notion title changed → push to Toggl) and
    eventually for archiving (active=false when a Notion project is
    finished). Toggl's PUT accepts a subset of project fields; we send only
    the ones the caller explicitly set so we never trample fields the team
    edited in Toggl's UI (color, billable, client_id, …).
    """
    body: dict[str, Any] = {}
    if name is not None:
        body["name"] = name
    if active is not None:
        body["active"] = active
    if not body:
        raise ValueError("update_project called with nothing to update")
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.put(
                f"/api/v9/workspaces/{workspace_id}/projects/{project_id}",
                json=body,
            ),
            op_name="update_project",
        )
        _raise_for_status(response)
        return response.json()
