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
    EMAILS_PROPS,
    PROJECTS_SYNC_PROGRESS_PROP,
    PROJECTS_SYNC_PROP,
    SYNC_QUEUE_PROPS,
    TASKS_PROPS,
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


def extract_page_title(page: dict[str, Any]) -> str | None:
    """Pull the title out of any page object — works for both DB rows and standalone pages."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title" and prop.get("title"):
            return "".join(t.get("plain_text", "") for t in prop["title"])
    return None


def read_relation_ids(page: dict[str, Any], prop_name: str) -> list[str]:
    """Read the related page IDs from a relation property on `page`."""
    prop = (page.get("properties") or {}).get(prop_name) or {}
    return [r.get("id", "") for r in (prop.get("relation") or []) if r.get("id")]


def task_project_id(page: dict[str, Any]) -> str | None:
    """Return the single project page id a task is related to, or None.

    A task should belong to exactly one project; if Notion has more than one
    related page we log and use the first — Notion's UI doesn't prevent it.
    """
    ids = read_relation_ids(page, TASKS_PROPS["project"])
    if len(ids) > 1:
        logger.warning(
            "task %s has %d Prosjekt relations; using the first (%s)",
            page.get("id"),
            len(ids),
            ids[0],
        )
    return ids[0] if ids else None


def task_discipline(page: dict[str, Any]) -> str | None:
    """Return the task's `Type` single-select option name, or None if unset.

    The raw label (e.g. "Interiør") is what callers pass to the NAS layer; it
    normalizes to a canonical key via DISCIPLINE_KEYS.
    """
    prop = (page.get("properties") or {}).get(TASKS_PROPS["discipline"]) or {}
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


async def tasks_for_project(project_page_id: str) -> list[dict[str, Any]]:
    """Every non-archived Oppgaver row that relates to this project, paginated.

    Used by the project-button flow to derive which discipline branches to
    create — the set of disciplines is "the union of Type across this
    project's tasks", so we don't need a separate discipline field on the
    project. Returns [] if TASKS_DB_ID is unset (feature disabled) or the
    project has no tasks yet.
    """
    if not settings.tasks_db_id:
        return []
    filter_body = {
        "filter": {
            "property": TASKS_PROPS["project"],
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
                f"/databases/{settings.tasks_db_id}/query", json=body
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


async def set_project_sync_status(project_page_id: str, option_name: str | None) -> None:
    """Set (or clear) the at-a-glance sync-status Select on a Projects-DB page.

    `option_name` is the literal Notion select option (e.g. "🟢 Active"); None
    clears the property. Caller is responsible for having created the property
    and its options in Notion — a missing property surfaces as a Notion 400,
    which the best-effort caller logs and ignores.
    """
    value: dict[str, Any] = (
        {"select": {"name": option_name}} if option_name else {"select": None}
    )
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}",
                json={"properties": {PROJECTS_SYNC_PROP: value}},
            ),
            op_name=f"PATCH /pages/{project_page_id} sync-status",
        )
        _raise_for_status(response)


async def set_project_sync_progress(project_page_id: str, text: str | None) -> None:
    """Set (or clear) the live "<done>/<total>" progress text on a Projects-DB page.

    Sibling to set_project_sync_status — the worker writes "11/23" here while a
    project's sync_tasks are in flight, and clears it (text=None) when the
    project goes idle. Best-effort: if the user hasn't added the property to
    Notion, the PATCH 400s and the caller logs+ignores (same pattern as the
    status writer above).
    """
    if text:
        value: dict[str, Any] = {"rich_text": [{"text": {"content": text}}]}
    else:
        value = {"rich_text": []}
    async with _client() as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/pages/{project_page_id}",
                json={"properties": {PROJECTS_SYNC_PROGRESS_PROP: value}},
            ),
            op_name=f"PATCH /pages/{project_page_id} sync-progress",
        )
        _raise_for_status(response)


# ============================================================
# Contacts DB
# ============================================================


async def find_contact_by_email(email: str) -> dict[str, Any] | None:
    """Query the Contacts DB for an existing contact with this email address."""
    if not settings.contacts_db_id:
        raise RuntimeError("CONTACTS_DB_ID is not configured")
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


async def append_blocks_to_page(page_id: str, blocks: list[dict[str, Any]]) -> None:
    """Append a list of Notion blocks to a page. Notion caps each call at 100 children.

    PATCH /blocks/.../children is the slowest Notion endpoint we hit in practice
    — sometimes 15-25s for a small block. We retry-with-backoff on transient
    timeouts; permanent failures (4xx) propagate immediately.
    """
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


def chunk_text(text: str, size: int = 1900) -> list[str]:
    """Split `text` into chunks of at most `size` chars. Notion's per-element max is 2000."""
    if not text:
        return [""]
    return [text[i : i + size] for i in range(0, len(text), size)]
