"""Async Notion REST client.

Surface area needed by the sync engine: Emails DB CRUD, Contacts DB upsert,
project page lookup, and generic block append for chat-style row bodies.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gb_automations.config import CONTACTS_PROPS, EMAILS_PROPS, settings

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
    """Wraps a Notion HTTP error with the response body so the actual cause is visible."""

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        message = f"Notion {response.status_code} {response.request.method} {response.url}: {body}"
        super().__init__(message)
        self.status_code = response.status_code
        self.body = body


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


async def search_pages(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every page the integration can see (top-level + DB rows)."""
    async with _client() as client:
        response = await client.post(
            "/search",
            json={"filter": {"value": "page", "property": "object"}, "page_size": page_size},
        )
        _raise_for_status(response)
        return response.json().get("results", [])


async def search_databases(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every database the integration can see. Use to discover DB IDs."""
    async with _client() as client:
        response = await client.post(
            "/search",
            json={"filter": {"value": "database", "property": "object"}, "page_size": page_size},
        )
        _raise_for_status(response)
        return response.json().get("results", [])


def extract_page_title(page: dict[str, Any]) -> str | None:
    """Pull the title out of any page object — works for both DB rows and standalone pages."""
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "title" and prop.get("title"):
            return "".join(t.get("plain_text", "") for t in prop["title"])
    return None


def extract_database_title(db: dict[str, Any]) -> str:
    """Pull the title out of a database object."""
    title_blocks = db.get("title", [])
    return "".join(t.get("plain_text", "") for t in title_blocks) or "(untitled)"


# ============================================================
# Project pages (top-level pages, used to match Gmail labels)
# ============================================================


async def get_project_pages() -> dict[str, dict[str, str]]:
    """Full Gmail-label path → {id, title, created_time}, for every Notion project page.

    The key is the *nested Gmail label name* (e.g. "Projects/2026/Acme") so it
    matches the actual label names on synced Gmail threads — the
    sync_thread → _pick_project intersection works directly without callers
    having to rebuild paths on each lookup.

    Value fields:
      - id:           the Notion page ID
      - title:        the raw page title (the leaf, e.g. "Acme") — used for logging
      - created_time: ISO 8601 from Notion; the year segment of the key derives
                      from this and is locked at project-creation time

    Excludes pages that are rows in the Contacts database OR in any year-
    partitioned Emails database. The Emails DB set is fetched from the local
    cache table populated by `notion_emails_db.get_emails_db_for_year`.
    """
    from gb_automations.clients import notion_emails_db
    from gb_automations.utils.labels import project_label_path

    contacts_id = settings.contacts_db_id.replace("-", "")
    emails_db_ids = await notion_emails_db.all_known_db_ids()
    out: dict[str, dict[str, str]] = {}

    pages = await search_pages()
    for page in pages:
        parent = page.get("parent") or {}
        if parent.get("type") == "database_id":
            parent_db = (parent.get("database_id") or "").replace("-", "").lower()
            if parent_db == contacts_id.lower() or parent_db in emails_db_ids:
                continue
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


async def create_contact(
    *, name: str, email: str, phone: str | None = None, company: str | None = None
) -> dict[str, Any]:
    """Create a new row in the Contacts DB. Returns the created page object."""
    if not settings.contacts_db_id:
        raise RuntimeError("CONTACTS_DB_ID is not configured")
    properties: dict[str, Any] = {
        CONTACTS_PROPS["name"]: {"title": [{"text": {"content": name}}]},
        CONTACTS_PROPS["email"]: {"email": email},
    }
    if phone:
        properties[CONTACTS_PROPS["phone"]] = {"phone_number": phone}
    if company:
        properties[CONTACTS_PROPS["company"]] = {"rich_text": [{"text": {"content": company}}]}
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
