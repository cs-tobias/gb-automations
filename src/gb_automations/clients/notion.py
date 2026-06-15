"""Async Notion REST client.

Surface area needed by the sync engine: Emails DB CRUD, Contacts DB upsert,
project page lookup, and generic block append for chat-style row bodies.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gb_automations.config import (
    COMPANIES_PROPS,
    CONTACTS_PROPS,
    DISCIPLINE_KEYS,
    EMAILS_PROPS,
    KORREKSJON_KIND_KORREKSJONSRUNDE,
    KORREKSJONER_PROPS,
    MANUAL_DELIVERABLE_STATUSES,
    OPPGAVER_FRAME_URL_PROP,
    OPPGAVER_PROPS,
    PROJECTS_FIKEN_PROPS,
    PROJECTS_FRAME_URL_PROP,
    PROJECTS_GMAIL_URL_PROP,
    PROJECTS_NAS_URL_PROP,
    PROJECTS_STATUS_PROP,
    PROJECTS_SYNC_PROGRESS_PROP,
    PROJECTS_SYNC_PROP,
    PROJECTS_TOGGL_URL_PROP,
    STATUS_KLAR_TIL_OPPSTART,
    SYNC_QUEUE_PROPS,
    settings,
)

logger = logging.getLogger(__name__)

NOTION_API_BASE = "https://api.notion.com/v1"
# Notion's write endpoints (PATCH /pages/.../children, PATCH /pages/...) can be
# slow when pages are just-created or the workspace is busy — we've observed
# real-world response times in the 15-25s range. Set the per-call budget to 30
# to cover those without timing out; retry-with-backoff on top catches the rest.
_HTTP_TIMEOUT = 30.0


async def _with_retries(
    operation: Callable[[], Awaitable[httpx.Response]],
    *,
    op_name: str,
    max_attempts: int = 3,
) -> httpx.Response:
    """Run `operation` with exponential backoff on transient httpx errors.

    Backoff: 1s, then 4s. Retries on ReadTimeout / ConnectTimeout / RemoteProtocolError
    / 5xx responses. Other errors propagate immediately (4xx is a real bug, not
    a transient).

    Notion's API docs explicitly recommend retrying on these failure modes;
    we've also observed real-world slow PATCH /blocks/.../children calls that
    timed out at 15s but would have succeeded at 30s.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await operation()
        except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as err:
            last_err = err
            if attempt + 1 >= max_attempts:
                break
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "notion %s attempt %d failed (%s); retrying in %.1fs",
                op_name,
                attempt + 1,
                type(err).__name__,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        # 5xx is also worth retrying (transient Notion-side issues).
        if 500 <= response.status_code < 600 and attempt + 1 < max_attempts:
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "notion %s attempt %d got %d; retrying in %.1fs",
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
    """httpx event hook — logs every outgoing Notion API call at DEBUG."""
    logger.debug("notion → %s %s", request.method, request.url.path)


async def _log_response(response: httpx.Response) -> None:
    """httpx event hook — logs the status of every Notion API call at DEBUG."""
    logger.debug(
        "notion ← %d %s %s",
        response.status_code,
        response.request.method,
        response.request.url.path,
    )


class NotionAPIError(RuntimeError):
    """Wraps a Notion HTTP error.

    `str(err)` is a single readable line (status + Notion's own message + the
    endpoint) for clean logs; `err.detail` holds the full body + URL for DEBUG.
    """

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        self.status_code = response.status_code
        self.body = body
        # Notion returns a human-readable `message`; surface just that on the
        # one-liner, plus the endpoint path (not the full URL with query).
        short = body.get("message") if isinstance(body, dict) else None
        method = response.request.method
        path = response.request.url.path
        self.summary = f"Notion {response.status_code} {method} {path}: {short or body}"
        self.detail = f"Notion {response.status_code} {method} {response.url}: {body}"
        super().__init__(self.summary)

    @property
    def is_stale_object(self) -> bool:
        """True when this error means the cached Notion id is no longer usable.

        Two distinct shapes, both meaning "the id we cached points at something
        gone or trashed — evict it and re-resolve":
          - 404 object_not_found: the page/db was deleted outright.
          - 400 with an "archived ancestor" message: the page itself still
            resolves, but its parent DB was archived/trashed, so it can't be
            edited. We've seen this when an old Emails DB is deleted but cached
            row ids linger.
        """
        if self.status_code == 404:
            return True
        if self.status_code == 400 and isinstance(self.body, dict):
            return "archived ancestor" in (self.body.get("message") or "").lower()
        return False


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise NotionAPIError(response)


def _headers() -> dict[str, str]:
    if not settings.notion_token:
        raise RuntimeError("NOTION_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": settings.notion_api_version,
        "Content-Type": "application/json",
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=NOTION_API_BASE,
        timeout=_HTTP_TIMEOUT,
        headers=_headers(),
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )


# ============================================================
# Generic / discovery
# ============================================================


async def _search_all(object_type: str, page_size: int) -> list[dict[str, Any]]:
    """Return EVERY /search result of the given object type, paginating fully.

    Notion's /search caps each response at page_size (max 100) and signals more
    via has_more + next_cursor. We MUST loop: a workspace with >100 visible
    objects (every synced Email/Contact row counts toward the page cap) would
    otherwise silently drop project pages past the first page, so
    get_project_pages couldn't match a thread to its project and sync_thread
    wrote nothing.
    """
    results: list[dict[str, Any]] = []
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            body: dict[str, Any] = {
                "filter": {"value": object_type, "property": "object"},
                "page_size": page_size,
            }
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await client.post("/search", json=body)
            _raise_for_status(response)
            payload = response.json()
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:  # defensive: has_more true but no cursor
                break
    return results


async def search_pages(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every page the integration can see (top-level + DB rows)."""
    return await _search_all("page", page_size)


async def search_databases(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every database the integration can see. Use to discover DB IDs."""
    return await _search_all("database", page_size)


async def list_workspace_users(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every workspace user via paginated `GET /v1/users`.

    Used by the Toggl hours engine to map a Toggl user (via their email) to
    the matching Notion user UUID, which then goes into the `Ansatt` people
    property on each Timer row.

    Response shape per user (type=person):
        {"object": "user", "id": "<uuid>", "type": "person",
         "person": {"email": "name@goldbox.no"}, "name": "Petter", ...}
    Bot users (type=bot) get returned too — caller filters on `type=="person"`
    because bots don't have `person.email` and can't own Timer rows.

    `/v1/users` paginates with query params (`start_cursor`, `page_size`),
    not POST body like `/search` and `/databases/{id}/query`.
    """
    results: list[dict[str, Any]] = []
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if start_cursor:
                params["start_cursor"] = start_cursor
            response = await _with_retries(
                lambda p=params: client.get("/users", params=p),
                op_name="GET /users",
            )
            _raise_for_status(response)
            payload = response.json()
            results.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:
                break
    return results


def extract_page_title(page: dict[str, Any]) -> str | None:
    """Pull the title out of any page object — works for both DB rows and standalone pages."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title" and prop.get("title"):
            return "".join(t.get("plain_text", "") for t in prop["title"])
    return None


def extract_project_status(page: dict[str, Any]) -> str | None:
    """Read the Projects DB row's `Status` option name, or None when unset.

    Reads BOTH `multi_select` and `select` (and Notion's newer `status` type).
    Goldbox runs `Status` as a multi_select but only ever sets one option per
    row — same pattern as `task_discipline` on Oppgaver. For multi_select we
    return the first non-empty option name; if the row has no options or the
    property is empty / unset, returns None.

    Returns None defensively for every shape where the option name is
    missing/empty so the caller can log a single "skipped: status not set"
    branch instead of guarding three layers.
    """
    prop = (page.get("properties") or {}).get(PROJECTS_STATUS_PROP) or {}

    multi = prop.get("multi_select")
    if isinstance(multi, list) and multi:
        for opt in multi:
            name = (opt.get("name") or "").strip() if isinstance(opt, dict) else ""
            if name:
                return name
        return None

    sel = prop.get("select") or prop.get("status") or {}
    name = sel.get("name") if isinstance(sel, dict) else None
    return name or None


def read_relation_ids(page: dict[str, Any], prop_name: str) -> list[str]:
    """Read the related page IDs from a relation property on `page`."""
    prop = (page.get("properties") or {}).get(prop_name) or {}
    return [r.get("id", "") for r in (prop.get("relation") or []) if r.get("id")]


def task_project_id(page: dict[str, Any]) -> str | None:
    """Return the single project page id an Oppgave row is related to, or None.

    A row should belong to exactly one project; if Notion has more than one
    related page we log and use the first — Notion's UI doesn't prevent it.
    """
    ids = read_relation_ids(page, OPPGAVER_PROPS["project"])
    if len(ids) > 1:
        logger.warning(
            "task %s has %d Prosjekt relations; using the first (%s)",
            page.get("id"),
            len(ids),
            ids[0],
        )
    return ids[0] if ids else None


def task_discipline(page: dict[str, Any]) -> str | None:
    """Return the row's `Type` option name (the discipline label), or None.

    Reads BOTH the `select` and `multi_select` shapes. Goldbox runs `Type` as a
    multi_select but only ever sets one label per row, so for multi_select we
    return the FIRST recognized discipline (an option whose normalized name is
    in DISCIPLINE_KEYS); if none match we fall back to the first option so the
    caller's own gate still does the final accept/reject. Single-select is read
    as before. The raw label (e.g. "Interiør") is what callers pass to the NAS
    layer; it normalizes to a canonical key via DISCIPLINE_KEYS.
    """
    prop = (page.get("properties") or {}).get(OPPGAVER_PROPS["discipline"]) or {}

    multi = prop.get("multi_select")
    if isinstance(multi, list) and multi:
        names = [(opt.get("name") or "").strip() for opt in multi]
        names = [n for n in names if n]
        for n in names:
            if DISCIPLINE_KEYS.get(n.lower()):
                return n
        return names[0] if names else None

    sel = prop.get("select") or {}
    name = sel.get("name")
    return name or None


def extract_database_title(db: dict[str, Any]) -> str:
    """Pull the title out of a database object."""
    title_blocks = db.get("title", [])
    return "".join(t.get("plain_text", "") for t in title_blocks) or "(untitled)"


def read_rich_text_prop(page: dict[str, Any], prop_name: str) -> str | None:
    """Read a rich_text (or title) property's plain text from a page object.

    Used to pull the `Thread ID` off an Emails-DB row when the client clicks a
    per-row "Re-sync" button. Returns None if the property is absent/empty.
    """
    prop = (page.get("properties") or {}).get(prop_name)
    if not prop:
        return None
    blocks = prop.get("rich_text") or prop.get("title") or []
    text = "".join(b.get("plain_text", "") for b in blocks).strip()
    return text or None


def read_number_prop(page: dict[str, Any], prop_name: str) -> float | None:
    """Read a `number` property from a page object. Returns None when unset.

    Used by the Fiken creation engine to read `Pris` (per-Oppgave unit
    price) and the two `*_fakturert andel` fractions. Notion stores
    numbers as JSON numbers (int or float); we always return float for
    uniform downstream math.
    """
    prop = (page.get("properties") or {}).get(prop_name)
    if not prop:
        return None
    value = prop.get("number")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def read_checkbox_prop(page: dict[str, Any], prop_name: str) -> bool:
    """Read a `checkbox` property from a page object. Returns False on
    absent/empty (Notion treats an unset checkbox as false).

    Used by the Fiken creation engine to read the per-Oppgave
    `Skal oppstartsfaktureres` / `Skal sluttfaktureres` picker checkboxes.
    """
    prop = (page.get("properties") or {}).get(prop_name)
    if not prop:
        return False
    return bool(prop.get("checkbox"))


def read_multi_select_names(page: dict[str, Any], prop_name: str) -> list[str]:
    """Read a `multi_select` property's option names from a page object.

    Returns a list of (stripped) names in the order Notion sends them.
    Empty list when the property is absent, empty, or not a multi_select.

    Used by the Fiken creation engine to read the `Fakturert` column —
    a row's billing state machine ("Oppstartsfakturert", "Sluttfakturert",
    "Sluttfakturert (full)", "Kreditert") lives there.
    """
    prop = (page.get("properties") or {}).get(prop_name)
    if not prop:
        return []
    options = prop.get("multi_select") or []
    names: list[str] = []
    for opt in options:
        if isinstance(opt, dict):
            name = (opt.get("name") or "").strip()
            if name:
                names.append(name)
    return names


def read_first_file_url(page: dict[str, Any], prop_name: str) -> str | None:
    """Return the URL of the first entry in a `files` property, or None.

    Handles both shapes Notion uses: a Notion-hosted upload (`file.url`, a
    time-limited signed S3 URL) and an external link (`external.url`). The
    placeholder render endpoint reads this on a fresh page fetch, so the signed
    URL is always current — callers must not cache it.
    """
    prop = (page.get("properties") or {}).get(prop_name)
    if not prop:
        return None
    for entry in prop.get("files") or []:
        url = (entry.get("file") or {}).get("url") or (
            entry.get("external") or {}
        ).get("url")
        if url:
            return url
    return None


# ============================================================
# Project pages (top-level pages, used to match Gmail labels)
# ============================================================

# Short-TTL cache for get_project_pages(). sync_thread calls it once per thread,
# and a queue drain / resync processes many threads back-to-back; without this
# each thread would re-query the project list. The list barely changes within one
# pass, so a few-second TTL collapses N lookups into one while staying fresh for a
# project created moments earlier.
_PROJECT_PAGES_TTL_SECONDS = 30.0
_project_pages_cache: dict[str, dict[str, str]] | None = None
_project_pages_cached_at: float = 0.0


async def _query_projects_db(db_id: str) -> list[dict[str, Any]]:
    """Every non-archived row in the Projects DB, paginated.

    Querying the one Projects DB is the cheap path: it holds only project rows
    (tens at most), versus /search which scans every page in the workspace —
    every synced Email and Contact row included. As the mailbox fills up, /search
    grows without bound and pages past the cap get dropped; the DB query does not.
    """
    rows: list[dict[str, Any]] = []
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await client.post(f"/databases/{db_id}/query", json=body)
            _raise_for_status(response)
            payload = response.json()
            rows.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:  # defensive: has_more true but no cursor
                break
    return [r for r in rows if not r.get("archived") and not r.get("in_trash")]


async def oppgaver_for_project(project_page_id: str) -> list[dict[str, Any]]:
    """Every non-archived Oppgaver row that relates to this project, paginated.

    Used by the project-button flow to derive which discipline branches to
    create — the set of disciplines is "the union of Type across this
    project's Oppgaver", so we don't need a separate discipline field on the
    project. Both deliverables and internal tasks contribute their discipline
    (intentional: a project's NAS branches should cover everything the team
    works on under it). Korreksjonsrunde sub-rows are excluded — their Type is
    "Korreksjonsrunde", not a discipline. Returns [] if OPPGAVER_DB_ID is
    unset (feature disabled) or the project has no Oppgaver yet.

    The Korreksjonsrunde exclusion is done CLIENT-SIDE, not via a Notion
    `select` filter: Goldbox runs `Type` as a multi_select, and Notion's API
    rejects a `select` filter clause on a multi_select property (400). Filtering
    on the relation server-side and dropping Korreksjonsrunde rows here is robust
    to either column type.
    """
    if not settings.oppgaver_db_id:
        return []
    filter_body = {
        "filter": {
            "property": OPPGAVER_PROPS["project"],
            "relation": {"contains": project_page_id},
        }
    }
    rows: list[dict[str, Any]] = []
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            body: dict[str, Any] = {"page_size": 100, **filter_body}
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await client.post(
                f"/databases/{settings.oppgaver_db_id}/query", json=body
            )
            _raise_for_status(response)
            payload = response.json()
            rows.extend(payload.get("results", []))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:
                break
    return [
        r
        for r in rows
        if not r.get("archived")
        and not r.get("in_trash")
        and task_discipline(r) != KORREKSJON_KIND_KORREKSJONSRUNDE
    ]


async def get_project_pages(*, force_refresh: bool = False) -> dict[str, dict[str, str]]:
    """Full Gmail-label path → {id, title, created_time}, for every Notion project page.

    The key is the *nested Gmail label name* (e.g. "Prosjekt/2026/Acme") so it
    matches the actual label names on synced Gmail threads — the
    sync_thread → _pick_projects intersection works directly without callers
    having to rebuild paths on each lookup.

    Value fields:
      - id:           the Notion page ID
      - title:        the raw page title (the leaf, e.g. "Acme") — used for logging
      - created_time: ISO 8601 from Notion; the year segment of the key derives
                      from this and is locked at project-creation time

    Source: when `projects_db_id` is configured we query that one database (cheap,
    bounded — see `_query_projects_db`). Only when it's unset (some local/dev
    workspaces) do we fall back to a full workspace /search, post-filtering out
    Contacts and year-partitioned Emails DB rows.

    Cached for `_PROJECT_PAGES_TTL_SECONDS`. Pass `force_refresh=True` to bypass
    the cache when a just-created project must be visible immediately.
    """
    global _project_pages_cache, _project_pages_cached_at

    now = time.monotonic()
    if (
        not force_refresh
        and _project_pages_cache is not None
        and now - _project_pages_cached_at < _PROJECT_PAGES_TTL_SECONDS
    ):
        return _project_pages_cache

    from gb_automations.utils.labels import project_label_path

    out: dict[str, dict[str, str]] = {}

    if settings.projects_db_id:
        pages = await _query_projects_db(settings.projects_db_id)
    else:
        # Dev/local fallback: no Projects DB configured, so scan everything and
        # drop the rows that are Contacts or Emails-DB entries.
        from gb_automations.clients import notion_emails_db

        contacts_id = settings.contacts_db_id.replace("-", "")
        emails_db_ids = await notion_emails_db.all_known_db_ids()
        pages = []
        for page in await search_pages():
            parent = page.get("parent") or {}
            if parent.get("type") == "database_id":
                parent_db = (parent.get("database_id") or "").replace("-", "").lower()
                if parent_db == contacts_id.lower() or parent_db in emails_db_ids:
                    continue
            pages.append(page)

    for page in pages:
        title = extract_page_title(page)
        if not title:
            continue
        created_time = page.get("created_time", "")
        label_path = project_label_path(title, created_time)
        out[label_path] = {
            "id": page["id"],
            "title": title,
            "created_time": created_time,
        }

    _project_pages_cache = out
    _project_pages_cached_at = now
    return out


# ============================================================
# Emails DB
# ============================================================


_emails_db_property_names_cache: dict[str, set[str]] = {}


async def get_emails_db_property_names(db_id: str) -> set[str]:
    """Names of every property on the given Emails DB, cached per-DB.

    With year-partitioned Emails DBs, callers pass the DB ID returned by
    `notion_emails_db.get_emails_db_for_year`. Each year DB caches separately
    (schemas are identical in practice but the API key is db_id, not year).
    Lets sync code skip setting properties that don't exist in the user's
    schema — so the same code adapts to different Notion workspaces without
    config changes.
    """
    if db_id in _emails_db_property_names_cache:
        return _emails_db_property_names_cache[db_id]
    if not db_id:
        raise RuntimeError("db_id is required (resolve via notion_emails_db first)")
    async with _client() as client:
        response = await client.get(f"/databases/{db_id}")
        _raise_for_status(response)
    names = set(response.json().get("properties", {}).keys())
    _emails_db_property_names_cache[db_id] = names
    return names


def reset_schema_cache() -> None:
    """Drop the cached schemas. Useful for tests or after manually editing a DB."""
    _emails_db_property_names_cache.clear()


async def get_page(page_id: str) -> dict[str, Any]:
    """Fetch a single page by ID."""
    async with _client() as client:
        response = await client.get(f"/pages/{page_id}")
        _raise_for_status(response)
        return response.json()


async def page_is_live(page_id: str) -> bool:
    """True if a page still exists and is editable (not deleted/archived/trashed).

    Used to validate a cached `notion_page_id` before trusting it: a deleted
    page 404s; an archived page (or one whose parent DB was trashed) comes back
    with archived/in_trash set. Either way the cached id is stale and should be
    evicted. A non-stale error (network/auth blip) is re-raised so we don't
    wrongly discard a good cache entry. Mirrors `notion_emails_db._database_exists`.
    """
    async with _client() as client:
        response = await client.get(f"/pages/{page_id}")
    try:
        _raise_for_status(response)
    except NotionAPIError as err:
        if err.is_stale_object:
            return False
        raise
    body = response.json()
    return not (body.get("archived") or body.get("in_trash"))


async def find_email_row_by_message_id(message_id: str, db_id: str) -> dict[str, Any] | None:
    """Query the given Emails DB for a non-archived row with this Gmail message ID.

    Caller passes `db_id` resolved via `notion_emails_db.get_emails_db_for_year`
    based on the message's year. Lookups only hit the year DB matching the
    message — wrong-year searches would always miss, so routing is correct
    by construction at the call sites.
    """
    if not db_id:
        raise RuntimeError("db_id is required (resolve via notion_emails_db first)")
    async with _client() as client:
        response = await client.post(
            f"/databases/{db_id}/query",
            json={
                "filter": {
                    "property": EMAILS_PROPS["message_id"],
                    "rich_text": {"equals": message_id},
                },
                "page_size": 5,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        live = [p for p in results if not p.get("archived") and not p.get("in_trash")]
        return live[0] if live else None


async def has_any_row_for_thread(thread_id: str, db_id: str) -> bool:
    """Quick existence check: any non-archived row with this Gmail thread ID
    in the given year DB. Threads spanning years need callers to query each
    year DB the thread might live in (or accept "only matters per-year")."""
    if not db_id:
        raise RuntimeError("db_id is required (resolve via notion_emails_db first)")
    async with _client() as client:
        response = await client.post(
            f"/databases/{db_id}/query",
            json={
                "filter": {
                    "property": EMAILS_PROPS["thread_id"],
                    "rich_text": {"equals": thread_id},
                },
                "page_size": 1,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        return any(not p.get("archived") and not p.get("in_trash") for p in results)


async def create_email_row(properties: dict[str, Any], db_id: str) -> dict[str, Any]:
    """Create a new row in the given Emails DB. Returns the created page object."""
    if not db_id:
        raise RuntimeError("db_id is required (resolve via notion_emails_db first)")
    async with _client() as client:
        response = await client.post(
            "/pages",
            json={
                "parent": {"database_id": db_id},
                "properties": properties,
            },
        )
        _raise_for_status(response)
        return response.json()


# The Oppgaver `Type` column is a single `select` in some workspaces and a
# `multi_select` in others (Goldbox). A write must match the column's actual
# type or Notion 400s, so we look the type up once and cache it. Cleared
# implicitly per process; the schema doesn't change at runtime.
_oppgaver_type_kind: str | None = None


async def _oppgaver_type_property_kind() -> str:
    """Return "multi_select" or "select" for the Oppgaver `Type` property.

    Cached after the first lookup. Defaults to "select" if the property or DB
    can't be read, preserving the historical behavior.
    """
    global _oppgaver_type_kind
    if _oppgaver_type_kind is not None:
        return _oppgaver_type_kind
    _oppgaver_type_kind = "select"
    if settings.oppgaver_db_id:
        async with _client() as client:
            response = await _with_retries(
                lambda: client.get(f"/databases/{settings.oppgaver_db_id}"),
                op_name="GET /databases/oppgaver (Type property kind)",
            )
            _raise_for_status(response)
            prop = (response.json().get("properties") or {}).get(
                OPPGAVER_PROPS["discipline"]
            ) or {}
            if prop.get("type") == "multi_select":
                _oppgaver_type_kind = "multi_select"
    return _oppgaver_type_kind


def _type_property_value(kind: str, option_name: str) -> dict[str, Any]:
    """Build a Type property write payload matching the column's kind."""
    if kind == "multi_select":
        return {"multi_select": [{"name": option_name}]}
    return {"select": {"name": option_name}}


async def create_korreksjonsrunde_row(
    *,
    deliverable_page_id: str,
    round_number: int,
    project_page_id: str | None = None,
) -> dict[str, Any]:
    """Create a "Korreksjonsrunde N" sub-row in the Oppgaver DB, parented
    (sub-item) under its deliverable. Returns the created page object.

    The round row carries Type=Korreksjonsrunde + Runde=N and sits as a
    sub-item of the deliverable via the OPPGAVER_PROPS["parent"] relation.
    It is the Notion anchor that the individual Korreksjon rows (in the
    Korreksjoner DB) relate back to.

    `project_page_id`, when set, writes the OPPGAVER_PROPS["project"]
    relation → Projects DB. Deliverable rows carry this relation, and the
    team's Oppgaver views group/filter by project; a round sub-row missing
    it falls out of those grouped views and reads as orphaned. Skipped
    silently if None.

    Caller is responsible for the dedup check (see find_korreksjonsrunde_row).
    Raises RuntimeError if OPPGAVER_DB_ID is unset.
    """
    if not settings.oppgaver_db_id:
        raise RuntimeError(
            "OPPGAVER_DB_ID is not set — Oppgaver writes are disabled."
        )
    type_kind = await _oppgaver_type_property_kind()
    properties: dict[str, Any] = {
        OPPGAVER_PROPS["name"]: {
            "title": [{"text": {"content": f"Korreksjonsrunde {round_number}"}}]
        },
        OPPGAVER_PROPS["kind"]: _type_property_value(
            type_kind, KORREKSJON_KIND_KORREKSJONSRUNDE
        ),
        OPPGAVER_PROPS["round"]: {"number": round_number},
        OPPGAVER_PROPS["parent"]: {"relation": [{"id": deliverable_page_id}]},
        # Round rows are only ever created on the first comment of the
        # round (see sync_frame_comments._ensure_korreksjonsrunde_oppgave),
        # so "comment present, nothing done" is always the right initial
        # state. The rollup engine (sync_leveranse_status) then walks this
        # status up to Under arbeid / Oppgaver ferdig as Korreksjon Ferdig
        # boxes get ticked off.
        OPPGAVER_PROPS["status"]: {"select": {"name": STATUS_KLAR_TIL_OPPSTART}},
    }
    if project_page_id:
        properties[OPPGAVER_PROPS["project"]] = {
            "relation": [{"id": project_page_id}]
        }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.post(
                "/pages",
                json={
                    "parent": {"database_id": settings.oppgaver_db_id},
                    "properties": properties,
                },
            ),
            op_name=f"POST /pages korreksjonsrunde(round={round_number})",
        )
        _raise_for_status(response)
        return response.json()


async def create_korreksjon_row(
    *,
    name: str,
    korreksjonsrunde_page_id: str,
    round_number: int,
    project_page_id: str | None = None,
    parent_korreksjon_id: str | None = None,
    commenter_contact_id: str | None = None,
    commented_at: str | None = None,
) -> dict[str, Any]:
    """Create a Korreksjon row (one per Frame comment) in the Korreksjoner DB.
    Returns the created page object.

    The row relates to its Korreksjonsrunde row (which lives in the Oppgaver
    DB) via KORREKSJONER_PROPS["korreksjonsrunde"] and carries Runde=N
    inherited from the round. There's no per-row Type: every row in the
    Korreksjoner DB is a Korreksjon by construction. `name` is the clean
    comment body (the commenter is recorded structurally, not prefixed in).

    `project_page_id`, when set, writes the KORREKSJONER_PROPS["project"]
    relation → Projects DB, denormalized onto the row so the feedback list is
    filterable/groupable by project. Set on top-level comments AND replies.
    Skipped silently if None (the row still lands; the relation is just empty).

    `parent_korreksjon_id`, when set, writes the self-referential
    KORREKSJONER_PROPS["parent"] relation so Notion renders this row as a
    sub-item — used for replies (a reply Korreksjon nests under the parent
    comment's Korreksjon, 3-level deep). Replies do NOT carry the
    Korreksjonsrunde relation: that keeps the round's direct-child count
    (count_korreksjon_children) free of replies.

    `commenter_contact_id`, when set, writes the KORREKSJONER_PROPS["commenter"]
    relation → Contacts DB (the find-or-created contact for the Frame author).
    The contact carries the name + email, so they aren't duplicated onto the
    row. Skipped silently if None.

    `commented_at`, when set, writes the KORREKSJONER_PROPS["commented_at"] date
    — the ISO timestamp of when the comment was made in Frame
    (comment.created_at). Skipped silently if None.

    Raises RuntimeError if KORREKSJONER_DB_ID is unset.
    """
    if not settings.korreksjoner_db_id:
        raise RuntimeError(
            "KORREKSJONER_DB_ID is not set — Korreksjoner writes are disabled."
        )
    properties: dict[str, Any] = {
        KORREKSJONER_PROPS["name"]: {"title": [{"text": {"content": name}}]},
        KORREKSJONER_PROPS["round"]: {"number": round_number},
    }
    if project_page_id is not None:
        properties[KORREKSJONER_PROPS["project"]] = {
            "relation": [{"id": project_page_id}]
        }
    if commenter_contact_id:
        properties[KORREKSJONER_PROPS["commenter"]] = {
            "relation": [{"id": commenter_contact_id}]
        }
    if commented_at:
        properties[KORREKSJONER_PROPS["commented_at"]] = {
            "date": {"start": commented_at}
        }
    if parent_korreksjon_id is not None:
        # Reply: nest under the parent comment, no direct round relation
        # (so it doesn't inflate the round's child count).
        properties[KORREKSJONER_PROPS["parent"]] = {
            "relation": [{"id": parent_korreksjon_id}]
        }
    else:
        # Top-level comment: direct child of the Korreksjonsrunde.
        properties[KORREKSJONER_PROPS["korreksjonsrunde"]] = {
            "relation": [{"id": korreksjonsrunde_page_id}]
        }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.post(
                "/pages",
                json={
                    "parent": {"database_id": settings.korreksjoner_db_id},
                    "properties": properties,
                },
            ),
            op_name=f"POST /pages korreksjon(round={round_number})",
        )
        _raise_for_status(response)
        return response.json()


async def find_korreksjonsrunde_row(
    deliverable_page_id: str, round_number: int
) -> str | None:
    """Find an existing "Korreksjonsrunde N" sub-row for a deliverable, or None.

    Queries the Oppgaver DB filtering on the sub-item parent relation
    (contains the deliverable) + Type=Korreksjonsrunde + Runde=N. Returns the
    page id of the first non-archived match, or None if the round hasn't been
    created yet (caller then creates via create_korreksjonsrunde_row).

    Returns None silently if OPPGAVER_DB_ID is unset — callers that depend on
    the lookup decision MUST also gate on the env, since "couldn't look up"
    and "doesn't exist" are not the same.
    """
    if not settings.oppgaver_db_id:
        return None
    # Filter only on type-stable properties server-side (parent relation +
    # round number). The Type/kind match is done client-side below: Goldbox
    # runs `Type` as a multi_select, and a `select` filter clause on a
    # multi_select property is rejected by Notion (400). task_discipline()
    # reads either shape, so the kind check works regardless of column type.
    conditions: list[dict[str, Any]] = [
        {
            "property": OPPGAVER_PROPS["parent"],
            "relation": {"contains": deliverable_page_id},
        },
        {
            "property": OPPGAVER_PROPS["round"],
            "number": {"equals": round_number},
        },
    ]
    body = {
        "filter": {"and": conditions},
        "page_size": 10,
    }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/databases/{settings.oppgaver_db_id}/query", json=body
            ),
            op_name=f"POST /databases/oppgaver/query lookup(korreksjonsrunde round={round_number})",
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
    for page in results:
        if page.get("archived") or page.get("in_trash"):
            continue
        # Client-side kind match (the Type column may be multi_select).
        if task_discipline(page) == KORREKSJON_KIND_KORREKSJONSRUNDE:
            return page.get("id")
    return None


async def patch_email_row_files(page_id: str, files: list[dict[str, str]]) -> None:
    """Set the Files property on an existing email row.

    `files` is a list of `{name, url}` dicts. Each entry becomes an external
    file in Notion's `files` property type. Capped at 100 entries per Notion's
    limit; filenames clipped to 100 chars.

    Called after the row is created (so a Drive failure doesn't block the
    row's existence). Uses the standard retry helper for transient timeouts.
    """
    if not files:
        return
    props = {
        EMAILS_PROPS["files"]: {
            "files": [
                {
                    "name": (f.get("name") or "attachment")[:100],
                    "external": {"url": f["url"]},
                }
                for f in files[:100]
            ]
        }
    }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(f"/pages/{page_id}", json={"properties": props}),
            op_name=f"PATCH /pages/{page_id} files",
        )
        _raise_for_status(response)


async def patch_email_row_project(page_id: str, project_page_ids: list[str]) -> None:
    """Set the Project relation on an existing email row. Idempotent.

    Called on every dedup-cache hit during sync_thread so that Gmail label
    swaps (user mislabels a thread, then corrects it) propagate to already-
    synced rows. PATCHing the same value is a no-op on Notion's side; the
    /webhooks/notion self-write filter swallows the resulting webhook echo.

    Pass an empty list to clear the relation (currently unused — see the
    "remove-only" follow-up in the original plan).
    """
    props = {
        EMAILS_PROPS["project"]: {
            "relation": [{"id": pid} for pid in project_page_ids]
        }
    }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(f"/pages/{page_id}", json={"properties": props}),
            op_name=f"PATCH /pages/{page_id} project",
        )
        _raise_for_status(response)


async def set_project_frame_url(project_page_id: str, url: str | None) -> None:
    """Write the Frame.io URL property on a Projects-DB page. Idempotent.

    Called by sync_frame after the project's Frame folder is created or
    confirmed live. Pass `url=None` to clear the property (e.g. when the
    Frame folder has been removed and we're not auto-recreating).

    PATCHing the same value is a no-op on Notion's side; the /webhooks/notion
    self-write filter swallows the resulting webhook echo so a Frame writeback
    doesn't loop a label_sync.
    """
    props = {PROJECTS_FRAME_URL_PROP: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{project_page_id} frame_url (project)",
        )
        _raise_for_status(response)


async def set_project_toggl_url(project_page_id: str, url: str | None) -> None:
    """Write the Toggl URL property on a Projects-DB page. Idempotent.

    Called by sync_toggl_project after the matching Toggl project is created
    or confirmed live. Same shape and rationale as set_project_frame_url —
    pass `url=None` to clear (e.g. if the Toggl project was deleted in the
    Toggl UI and we want the column to reflect "not provisioned" until the
    next sync repairs it).
    """
    props = {PROJECTS_TOGGL_URL_PROP: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{project_page_id} toggl_url (project)",
        )
        _raise_for_status(response)


async def set_deliverable_frame_url(deliverable_page_id: str, url: str | None) -> None:
    """Write the Frame.io URL property on a deliverable row (Oppgaver DB).
    Idempotent.

    Same shape and rationale as set_project_frame_url, just for a deliverable
    row. Pass `url=None` to clear (e.g. if the deliverable's Frame folder is
    deleted manually and we want the property to reflect "not provisioned").
    """
    props = {OPPGAVER_FRAME_URL_PROP: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{deliverable_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{deliverable_page_id} frame_url (deliverable)",
        )
        _raise_for_status(response)


# ============================================================
# Fiken — billing-state writebacks on Oppgaver + Project rows
# ============================================================


async def set_oppgave_billing_state(
    oppgave_page_id: str,
    *,
    invoice_type: str,
    label_to_add: str,
    existing_labels: list[str],
) -> None:
    """Stamp billing state on an Oppgave after a Fiken draft is created.

    Atomic on Notion's side (single PATCH carrying both updates), so we
    never leave a row half-stamped — the `Fakturert` multi-select picks
    up the new label AND the corresponding `Skal …` checkbox clears,
    or neither.

    `invoice_type` is "oppstart" or "slutt". The corresponding
    `Skal …faktureres` checkbox is cleared.

    `label_to_add` is the Fakturert label the caller decided to add
    (e.g. "Oppstartsfakturert", "Sluttfakturert", "Sluttfakturert (full)").
    Engine code computes this from the row's CURRENT labels — slutt
    after a prior oppstart adds "Sluttfakturert"; slutt on a fresh row
    adds "Sluttfakturert (full)". This helper is content-agnostic; it
    just unions `label_to_add` into the existing set so labels never
    overwrite each other.

    `existing_labels` is the row's current multi-select option names,
    read by the caller (typically from a `get_page` immediately
    beforehand). Passing them in keeps this helper a pure write — no
    extra read here for the racy "labels could change between our
    re-read and the PATCH" case.

    Idempotent at the API level (PATCHing the same union is a no-op).
    """
    if invoice_type == "oppstart":
        checkbox_prop = OPPGAVER_PROPS["should_invoice_at_start"]
    elif invoice_type == "slutt":
        checkbox_prop = OPPGAVER_PROPS["should_invoice_at_end"]
    else:
        raise ValueError(f"invoice_type must be oppstart|slutt, got {invoice_type!r}")

    fakturert_prop = OPPGAVER_PROPS["billed_status"]

    # Union the new label with what's already there. Dedup by name
    # (preserve order: existing first, then new if not present).
    merged: list[str] = list(existing_labels)
    if label_to_add and label_to_add not in merged:
        merged.append(label_to_add)
    multi_select_payload = [{"name": name} for name in merged if name]

    props = {
        fakturert_prop: {"multi_select": multi_select_payload},
        checkbox_prop: {"checkbox": False},
    }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{oppgave_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{oppgave_page_id} fiken billing state ({invoice_type})",
        )
        _raise_for_status(response)


async def set_project_last_draft_url(project_page_id: str, url: str | None) -> None:
    """Write the `Siste fiken-utkast` URL property on a Projects-DB page.
    Idempotent.

    Called by sync_fiken_invoice after a successful draft creation so the
    user can jump straight to the draft from the project row. Pass
    `url=None` to clear.
    """
    props = {PROJECTS_FIKEN_PROPS["last_draft_url"]: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{project_page_id} last_draft_url",
        )
        _raise_for_status(response)


# ============================================================
# Phase 2.5 — Deliverable Status + Ferdig helpers
# ============================================================


async def get_oppgave_status(page_id: str) -> str | None:
    """Read the current `Status` select option name on ANY Oppgaver row
    (deliverable, Korreksjonsrunde sub-row, or internal task).

    Returns the option name (e.g. "Klar til oppstart") or None if the
    property is empty / the page doesn't have a Status set yet. Used as
    the read-first half of the "skip if same / skip if manual" gate in
    `set_oppgave_status`.
    """
    page = await get_page(page_id)
    prop = (page.get("properties") or {}).get(OPPGAVER_PROPS["status"]) or {}
    sel = prop.get("select") or {}
    name = sel.get("name")
    return name or None


async def set_oppgave_status(page_id: str, status_name: str) -> str:
    """Set the `Status` select on ANY Oppgaver row by page id. Single
    chokepoint for auto-writes against the Oppgaver DB's Status column
    — used for deliverables AND for Korreksjonsrunde sub-rows.

    Returns the action taken: "written" | "unchanged" | "skipped_manual".

    Three-way guard:
      1. If the current status is in MANUAL_DELIVERABLE_STATUSES
         (Trenger avklaring, Utgår), return "skipped_manual" without
         writing. The team's manual override always wins; they have to
         move the status out of these states themselves.
      2. If the current status already equals `status_name`, return
         "unchanged" without writing.
      3. Otherwise PATCH the page and return "written".

    The read-first behavior is also the loop-prevention guard for the
    cascading webhooks that fire when this status flips.
    """
    current = await get_oppgave_status(page_id)
    if current in MANUAL_DELIVERABLE_STATUSES:
        logger.info(
            "oppgave_status: skipped (current=%r is a manual state) on %s",
            current,
            page_id,
        )
        return "skipped_manual"
    if current == status_name:
        return "unchanged"
    props = {OPPGAVER_PROPS["status"]: {"select": {"name": status_name}}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{page_id} status (oppgave)",
        )
        _raise_for_status(response)
    logger.info(
        "oppgave_status: %s → %r on %s",
        current or "(empty)",
        status_name,
        page_id,
    )
    return "written"


async def force_set_oppgave_status(
    page_id: str, status_name: str | None
) -> str:
    """Set (or CLEAR with `status_name=None`) the Status select on an
    Oppgaver row, bypassing the manual-override guard.

    Used by the Utgår reconcile engine (sync/sync_frame_file_status.py)
    when Frame.io is the source of truth — the Frame UI cleared its
    Status, so Notion's Utgår needs to clear too. The normal
    `set_oppgave_status` refuses to overwrite manual states, which is
    correct for ROLLUP-driven writes but wrong for an explicit Frame ↔
    Notion mirror where the team's intent is to move the manual state.

    Returns "written" | "unchanged". No "skipped_manual" branch by
    design — caller has already decided this is an explicit move.
    `status_name=None` clears the property (Notion's PATCH accepts
    `{"select": null}`).
    """
    current = await get_oppgave_status(page_id)
    if current == status_name:
        return "unchanged"
    props: dict[str, Any] = {
        OPPGAVER_PROPS["status"]: {"select": {"name": status_name} if status_name else None}
    }
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{page_id} status (force)",
        )
        _raise_for_status(response)
    logger.info(
        "oppgave_status: FORCE %s → %r on %s",
        current or "(empty)",
        status_name,
        page_id,
    )
    return "written"


# Back-compat aliases: every existing call site in the codebase uses
# these names. The generalized helpers above do exactly what the
# deliverable-specific versions did; the rename is purely semantic so
# Korreksjonsrunde callers don't have to import a function named for
# deliverables.
get_deliverable_status = get_oppgave_status
set_deliverable_status = set_oppgave_status


async def get_row_done(page_id: str) -> bool:
    """Read the current `Ferdig` checkbox state on a Korreksjon row
    (Korreksjoner DB). The Oppgaver DB no longer has a Ferdig checkbox, so
    this is only ever called on Korreksjon rows."""
    page = await get_page(page_id)
    prop = (page.get("properties") or {}).get(KORREKSJONER_PROPS["done"]) or {}
    return bool(prop.get("checkbox"))


async def set_row_done(page_id: str, done: bool) -> str:
    """Set the `Ferdig` checkbox on a Korreksjon row (Korreksjoner DB).
    Idempotent.

    Returns "written" | "unchanged". Read-first to skip same-value
    writes — this is the loop-prevention guard for the Notion ↔ Frame
    bidirectional sync: when our engine sets Notion to mirror Frame's
    state, Notion's automation will fire back; the resulting
    sync_oppgave_done call reads Frame, sees it already matches the
    new Notion checkbox, and skips the PATCH back. No infinite loop.
    """
    current = await get_row_done(page_id)
    if current == done:
        return "unchanged"
    props = {KORREKSJONER_PROPS["done"]: {"checkbox": done}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{page_id} done",
        )
        _raise_for_status(response)
    logger.info(
        "row_done: %s → %s on %s",
        current,
        done,
        page_id,
    )
    return "written"


async def count_korreksjon_children(
    korreksjonsrunde_page_id: str,
) -> tuple[int, int]:
    """Return (total, done_count) for the Korreksjon rows of a Korreksjonsrunde.

    Used by the status-rollup engine: count the per-comment Korreksjon rows
    (Korreksjoner DB) related to a Korreksjonsrunde row (Oppgaver DB) to
    decide between `Under arbeid` (some done) and `Oppgaver ferdig` (all
    done).

    Returns (0, 0) when KORREKSJONER_DB_ID is unset or the round has no
    Korreksjoner — caller treats both as "leave status alone". Filter:
    the `Korreksjonsrunde` relation contains `korreksjonsrunde_page_id`.
    Reply Korreksjon rows are excluded automatically: replies don't carry
    the round relation (they nest under their parent comment via Parent
    item), so they never match this filter.
    """
    if not settings.korreksjoner_db_id:
        return 0, 0
    body = {
        "filter": {
            "property": KORREKSJONER_PROPS["korreksjonsrunde"],
            "relation": {"contains": korreksjonsrunde_page_id},
        },
        "page_size": 100,
    }
    total = 0
    done = 0
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await _with_retries(
                lambda: client.post(
                    f"/databases/{settings.korreksjoner_db_id}/query", json=body
                ),
                op_name="POST /databases/korreksjoner/query count_korreksjon_children",
            )
            _raise_for_status(response)
            payload = response.json()
            for row in payload.get("results", []):
                if row.get("archived") or row.get("in_trash"):
                    continue
                total += 1
                props = row.get("properties") or {}
                done_prop = props.get(KORREKSJONER_PROPS["done"]) or {}
                if done_prop.get("checkbox"):
                    done += 1
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:
                break
    return total, done


async def set_project_gmail_url(project_page_id: str, url: str | None) -> None:
    """Write the Gmail URL property on a Projects-DB page. Idempotent.

    Called by sync_project_labels once the project's Gmail label is created or
    confirmed live across mailboxes. The URL points at the generic /u/0/ label
    path, so it opens for whichever Goldbox mailbox the team member happens to
    be signed into. Pass `url=None` to clear (e.g. for an "unprovisioned"
    visual when the label was deleted in every mailbox).
    """
    props = {PROJECTS_GMAIL_URL_PROP: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{project_page_id} gmail_url (project)",
        )
        _raise_for_status(response)


async def set_project_nas_url(project_page_id: str, url: str | None) -> None:
    """Write the NAS path property on a Projects-DB page. Idempotent.

    Despite the name, this is a plain URL property in Notion — Notion accepts
    arbitrary text in URL fields, including Windows-style `W:\\Prosjekt\\...`
    paths. The team can copy the value and paste it straight into File
    Explorer. Pass `url=None` to clear.
    """
    props = {PROJECTS_NAS_URL_PROP: {"url": url}}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{project_page_id} nas_url (project)",
        )
        _raise_for_status(response)


async def archive_page(page_id: str) -> None:
    """Archive (trash) a Notion page. Idempotent — re-archiving is a no-op.

    Used by the project resync to take down stale email rows before re-creating
    them. `{"archived": true}` moves the page to Notion's trash (recoverable for
    ~30 days), which is enough for our dedup readers — `find_email_row_by_message_id`
    and `has_any_row_for_thread` both filter out `archived`/`in_trash` pages, so
    the row becomes invisible and re-sync recreates it fresh.

    When the page is ALREADY archived, Notion returns 400 "Can't edit block
    that is archived. You must unarchive the block before editing." That's
    semantically a no-op success for us (goal: page archived → already met),
    so we swallow that specific 400 rather than treating it as an error. Lets
    `resync_project` survive an old list of page ids that includes pages a
    previous resync already archived (which used to log noisy ERROR + traceback
    on every duplicate-archive attempt).
    """
    async with _client() as client:
        try:
            response = await _with_retries(
                lambda: client.patch(f"/pages/{page_id}", json={"archived": True}),
                op_name=f"PATCH /pages/{page_id} archive",
            )
            _raise_for_status(response)
        except NotionAPIError as err:
            # 400 with "archived" in the message = page is already archived.
            # Treat as success; any other 400/5xx still propagates.
            if (
                err.status_code == 400
                and isinstance(err.body, dict)
                and "archived" in (err.body.get("message") or "").lower()
            ):
                logger.debug("archive_page: %s already archived — no-op", page_id)
                return
            raise


# ============================================================
# Sync Queue DB (live mirror of the durable Postgres queue)
# ============================================================


async def find_sync_queue_row(thread_id: str) -> dict[str, Any] | None:
    """Find the existing mirror row for a thread, if any (dedup key = Thread ID)."""
    if not settings.sync_queue_db_id:
        return None
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.sync_queue_db_id}/query",
            json={
                "filter": {
                    "property": SYNC_QUEUE_PROPS["thread_id"],
                    "rich_text": {"equals": thread_id},
                },
                "page_size": 1,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None


async def upsert_sync_queue_row(
    *,
    thread_id: str,
    subject: str,
    status: str,
    project: str | None = None,
    attempts: int | None = None,
    error: str | None = None,
    queued_at_iso: str | None = None,
) -> None:
    """Create or update the mirror row for a thread, keyed by Thread ID.

    One row per active queue task. Caller passes the status label
    (SYNC_QUEUE_STATUS_*). No-op when sync_queue_db_id is unset.
    """
    if not settings.sync_queue_db_id:
        return
    properties: dict[str, Any] = {
        SYNC_QUEUE_PROPS["subject"]: {"title": [{"text": {"content": subject or "(no subject)"}}]},
        SYNC_QUEUE_PROPS["status"]: {"select": {"name": status}},
        SYNC_QUEUE_PROPS["thread_id"]: {"rich_text": [{"text": {"content": thread_id}}]},
    }
    if project:
        properties[SYNC_QUEUE_PROPS["project"]] = {
            "rich_text": [{"text": {"content": project}}]
        }
    if attempts is not None:
        properties[SYNC_QUEUE_PROPS["attempts"]] = {"number": attempts}
    if error:
        properties[SYNC_QUEUE_PROPS["error"]] = {"rich_text": [{"text": {"content": error[:1900]}}]}
    if queued_at_iso:
        properties[SYNC_QUEUE_PROPS["queued_at"]] = {"date": {"start": queued_at_iso}}

    existing = await find_sync_queue_row(thread_id)
    async with _client() as client:
        if existing:
            response = await client.patch(
                f"/pages/{existing['id']}", json={"properties": properties}
            )
        else:
            response = await client.post(
                "/pages",
                json={
                    "parent": {"database_id": settings.sync_queue_db_id},
                    "properties": properties,
                },
            )
        _raise_for_status(response)


async def remove_sync_queue_row(thread_id: str) -> None:
    """Archive the mirror row for a thread (it's a live worklist, not a log).

    No-op when sync_queue_db_id is unset or no row exists.
    """
    if not settings.sync_queue_db_id:
        return
    existing = await find_sync_queue_row(thread_id)
    if not existing:
        return
    await archive_page(existing["id"])


_PROGRESS_UNCHANGED = object()  # sentinel: "don't write the progress field this call"


async def set_project_sync_state(
    project_page_id: str,
    *,
    status_option: str | None,
    progress: str | None | object = _PROGRESS_UNCHANGED,
) -> None:
    """Write the project's at-a-glance sync state to Notion in ONE PATCH.

    Combines the icon (PROJECTS_SYNC_PROP Select) and the live "<done>/<total>"
    counter (PROJECTS_SYNC_PROGRESS_PROP rich_text) into a single request, so
    Notion commits them atomically — no half-second lag where the icon updates
    but the counter trails behind.

    Args:
      status_option: Notion select option name (e.g. "🔄") or None to clear.
      progress: text like "5/17" to write, None to CLEAR the field, or the
        sentinel `_PROGRESS_UNCHANGED` (default) to NOT include it in the
        PATCH at all. The sentinel matters because Notion treats an absent
        key as "leave alone" but a present key with empty rich_text as
        "clear this field" — three distinct intents, two payload shapes.

    Caller is responsible for having created both properties in Notion; a
    missing one surfaces as a 400 which the best-effort caller logs+ignores.
    """
    props: dict[str, Any] = {
        PROJECTS_SYNC_PROP: (
            {"select": {"name": status_option}} if status_option else {"select": None}
        ),
    }
    if progress is not _PROGRESS_UNCHANGED:
        if progress:
            props[PROJECTS_SYNC_PROGRESS_PROP] = {
                "rich_text": [{"text": {"content": progress}}]  # type: ignore[dict-item]
            }
        else:
            props[PROJECTS_SYNC_PROGRESS_PROP] = {"rich_text": []}

    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}",
                json={"properties": props},
            ),
            op_name=f"PATCH /pages/{project_page_id} sync-state",
        )
        _raise_for_status(response)


# ============================================================
# Contacts DB
# ============================================================


async def find_contact_by_email(email: str) -> dict[str, Any] | None:
    """Query the Contacts DB for an existing contact with this email address.

    Lowercased internally so callers can't accidentally create casing-based
    duplicates. The Gmail path already normalizes upstream (`Participant.email`
    is documented as always-lowercased); the Frame path now does too; this is
    the chokepoint that closes the loop for any future caller.
    """
    if not settings.contacts_db_id:
        raise RuntimeError("CONTACTS_DB_ID is not configured")
    email = email.strip().lower()
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.contacts_db_id}/query",
            json={
                "filter": {"property": CONTACTS_PROPS["email"], "email": {"equals": email}},
                "page_size": 1,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None


async def find_contact_by_name_exact(name: str) -> dict[str, Any] | None:
    """Find a contact by exact-string Name match.

    Returns the row only if exactly ONE contact has this name; returns None
    on zero matches OR on multiple matches. Ambiguous matches are treated as
    a miss so the caller falls through to "create new row" rather than
    guessing which existing person to attach to — a wrong guess silently
    attaches emails to the wrong contact, which is the failure mode we most
    want to avoid.

    Used as a fallback after `find_contact_by_email` misses, so a manually-
    created contact row (with a Name but no Email yet) can be matched and
    have its empty Email field filled — additively — rather than producing
    a duplicate row.
    """
    if not settings.contacts_db_id:
        raise RuntimeError("CONTACTS_DB_ID is not configured")
    if not name.strip():
        return None
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.contacts_db_id}/query",
            json={
                "filter": {"property": CONTACTS_PROPS["name"], "title": {"equals": name}},
                "page_size": 2,  # enough to detect "more than one"
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        if len(results) == 1:
            return results[0]
        return None


async def create_contact(
    *,
    name: str,
    email: str,
    phone: str | None = None,
    title: str | None = None,
    address: str | None = None,
    company_page_id: str | None = None,
) -> dict[str, Any]:
    """Create a new row in the Contacts DB. Returns the created page object.

    `company_page_id` is the Notion page ID of the related Company row;
    when provided, it populates the single-relation. The old free-text
    `Company` property is gone — pass nothing (don't fall back to a name
    guess here; the caller is responsible for resolving + upserting the
    Company first).

    `Company Status` is intentionally absent: it's managed manually in Notion.
    """
    if not settings.contacts_db_id:
        raise RuntimeError("CONTACTS_DB_ID is not configured")
    # Lowercased so two callsites passing different casings for the same
    # address can't produce two visually-distinct rows. Mirrors the
    # normalization in `find_contact_by_email`.
    email = email.strip().lower()
    properties: dict[str, Any] = {
        CONTACTS_PROPS["name"]: {"title": [{"text": {"content": name}}]},
        CONTACTS_PROPS["email"]: {"email": email},
    }
    if phone:
        properties[CONTACTS_PROPS["phone"]] = {"phone_number": phone}
    if title:
        properties[CONTACTS_PROPS["title"]] = {"rich_text": [{"text": {"content": title}}]}
    if address:
        properties[CONTACTS_PROPS["address"]] = {"rich_text": [{"text": {"content": address}}]}
    if company_page_id:
        properties[CONTACTS_PROPS["company"]] = {"relation": [{"id": company_page_id}]}
    async with _client() as client:
        response = await client.post(
            "/pages",
            json={
                "parent": {"database_id": settings.contacts_db_id},
                "properties": properties,
            },
        )
        _raise_for_status(response)
        return response.json()


async def patch_contact(page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Patch any subset of a contact's properties."""
    async with _client() as client:
        response = await client.patch(f"/pages/{page_id}", json={"properties": properties})
        _raise_for_status(response)
        return response.json()


async def update_contact_phone(page_id: str, phone: str) -> None:
    """Convenience wrapper for the common "add phone if missing" case."""
    await patch_contact(page_id, {CONTACTS_PROPS["phone"]: {"phone_number": phone}})


async def patch_contact_enrichment(
    page_id: str,
    *,
    existing_props: dict[str, Any] | None = None,
    email: str | None = None,
    phone: str | None = None,
    title: str | None = None,
    address: str | None = None,
    company_page_id: str | None = None,
) -> None:
    """Fill blank enrichment fields on an existing contact; never overwrite.

    The "additive-only, everywhere" rule: we read each property's current
    value from Notion and only write fields that are empty there. Applies
    to every field including Email — if Goldbox has a manually-created row
    with a Name but no Email, this fills the Email; if they already have an
    email there, we leave it. Same rule across phone/title/address/company.
    Goldbox can edit any field manually without us clobbering their work.

    Pass `existing_props` (the `properties` dict from a prior find/query) to
    avoid an extra round-trip; otherwise this issues a fresh GET.
    """
    if not any([email, phone, title, address, company_page_id]):
        return

    if existing_props is None:
        async with _client() as client:
            response = await client.get(f"/pages/{page_id}")
            _raise_for_status(response)
            existing_props = response.json().get("properties", {})

    to_write: dict[str, Any] = {}

    if email and not _has_email(existing_props.get(CONTACTS_PROPS["email"])):
        to_write[CONTACTS_PROPS["email"]] = {"email": email}
    if phone and not _has_phone(existing_props.get(CONTACTS_PROPS["phone"])):
        to_write[CONTACTS_PROPS["phone"]] = {"phone_number": phone}
    if title and not _has_rich_text(existing_props.get(CONTACTS_PROPS["title"])):
        to_write[CONTACTS_PROPS["title"]] = {"rich_text": [{"text": {"content": title}}]}
    if address and not _has_rich_text(existing_props.get(CONTACTS_PROPS["address"])):
        to_write[CONTACTS_PROPS["address"]] = {"rich_text": [{"text": {"content": address}}]}
    if company_page_id and not _has_relation(existing_props.get(CONTACTS_PROPS["company"])):
        to_write[CONTACTS_PROPS["company"]] = {"relation": [{"id": company_page_id}]}

    if to_write:
        await patch_contact(page_id, to_write)


def _has_email(prop: dict[str, Any] | None) -> bool:
    return bool(prop and prop.get("email"))


def contact_email_value(props: dict[str, Any] | None) -> str | None:
    """The Email value on a contact's `properties` dict, or None if unset.

    Lets callers (sync_thread's name-match path) compare an existing row's
    email against an incoming one without re-querying Notion.
    """
    if not props:
        return None
    email = (props.get(CONTACTS_PROPS["email"]) or {}).get("email")
    return email or None


def _has_phone(prop: dict[str, Any] | None) -> bool:
    return bool(prop and prop.get("phone_number"))


def _has_rich_text(prop: dict[str, Any] | None) -> bool:
    return bool(prop and prop.get("rich_text"))


def _has_relation(prop: dict[str, Any] | None) -> bool:
    return bool(prop and prop.get("relation"))


# ============================================================
# Companies DB
# ============================================================


async def find_company_by_domain(domain: str) -> dict[str, Any] | None:
    """Query the Companies DB for an existing row whose Domain matches.

    Dedup is by email domain, not by Name — see COMPANIES_PROPS in config.py
    for the reasoning. Domain is rich_text in Notion, so we use the equals
    filter on rich_text.
    """
    if not settings.companies_db_id:
        raise RuntimeError("COMPANIES_DB_ID is not configured")
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.companies_db_id}/query",
            json={
                "filter": {
                    "property": COMPANIES_PROPS["domain"],
                    "rich_text": {"equals": domain},
                },
                "page_size": 1,
            },
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
        return results[0] if results else None


async def create_company(*, name: str, domain: str) -> dict[str, Any]:
    """Create a new row in the Companies DB. Returns the created page object."""
    if not settings.companies_db_id:
        raise RuntimeError("COMPANIES_DB_ID is not configured")
    properties: dict[str, Any] = {
        COMPANIES_PROPS["name"]: {"title": [{"text": {"content": name}}]},
        COMPANIES_PROPS["domain"]: {"rich_text": [{"text": {"content": domain}}]},
    }
    async with _client() as client:
        response = await client.post(
            "/pages",
            json={
                "parent": {"database_id": settings.companies_db_id},
                "properties": properties,
            },
        )
        _raise_for_status(response)
        return response.json()


# ============================================================
# Block helpers (for chat-style email row bodies)
# ============================================================


async def append_blocks_to_page(
    page_id: str, blocks: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append a list of Notion blocks to a page. Notion caps each call at 100 children.

    PATCH /blocks/.../children is the slowest Notion endpoint we hit in practice
    — sometimes 15-25s for a small block. We retry-with-backoff on transient
    timeouts; permanent failures (4xx) propagate immediately.

    Returns the concatenated list of created block objects across all batches
    (each carries an `id` that callers can persist when they need to later
    PATCH children under it — Phase 2 reply indenting uses this to find a
    parent comment's bullet block by id without re-listing the whole page).
    Existing call sites that ignore the return value continue to work.
    """
    created: list[dict[str, Any]] = []
    async with _client() as client:
        # Chunk into batches of 100 to respect Notion's limit.
        for i in range(0, len(blocks), 100):
            batch = blocks[i : i + 100]
            response = await _with_retries(
                lambda batch=batch: client.patch(
                    f"/blocks/{page_id}/children", json={"children": batch}
                ),
                op_name=f"PATCH /blocks/{page_id}/children",
            )
            _raise_for_status(response)
            created.extend(response.json().get("results", []))
    return created


def paragraph_block(text: str) -> dict[str, Any]:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": [{"type": "text", "text": {"content": text}}]},
    }


def callout_block(
    *, title: str, body: str, icon: str = "📨", color: str = "gray_background"
) -> dict[str, Any]:
    """Chat-style callout used to render one email message in a row's page body.

    Notion caps rich_text content at 2000 chars per element — caller is responsible
    for chunking if `body` is longer (see chunk_text).
    """
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "icon": {"type": "emoji", "emoji": icon},
            "color": color,
            "rich_text": [
                {
                    "type": "text",
                    "text": {"content": title + "\n"},
                    "annotations": {"bold": True},
                },
                {"type": "text", "text": {"content": body}},
            ],
        },
    }


def bullet_block(
    text: str, *, children: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Build a bulleted_list_item block with optional nested children.

    Used by the Phase 2 Frame-comments engine to render a comment as one
    bullet on a Korreksjonsrunde Oppgave's page body. `children` (when set)
    becomes the nested bullet list directly below — Frame comment replies
    use this on first-render to indent under their parent.

    Notion's API allows arbitrary nesting depth via either inline `children`
    on the block or a subsequent PATCH `/blocks/{parent}/children`. The
    engine uses the inline form for replies that arrive *with* the parent,
    and the PATCH form for replies that arrive later (looked up via
    `FrameComment.notion_block_id`). Caller is responsible for chunking
    `text` if it exceeds Notion's 2000-char rich_text limit.
    """
    block: dict[str, Any] = {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {
            "rich_text": [{"type": "text", "text": {"content": text}}],
        },
    }
    if children:
        block["bulleted_list_item"]["children"] = children
    return block


def chunk_text(text: str, size: int = 1900) -> list[str]:
    """Split `text` into chunks of at most `size` chars. Notion's per-element max is 2000."""
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]
