"""Async Notion REST client.

Surface area needed by the sync engine: Emails DB CRUD, Contacts DB upsert,
project page lookup, and generic block append for chat-style row bodies.
"""

from typing import Any

import httpx

from gb_automations.config import CONTACTS_PROPS, EMAILS_PROPS, settings

NOTION_API_BASE = "https://api.notion.com/v1"
_HTTP_TIMEOUT = 15.0


def _headers() -> dict[str, str]:
    if not settings.notion_token:
        raise RuntimeError("NOTION_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": settings.notion_api_version,
        "Content-Type": "application/json",
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=NOTION_API_BASE, timeout=_HTTP_TIMEOUT, headers=_headers())


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
        response.raise_for_status()
        return response.json().get("results", [])


async def search_databases(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every database the integration can see. Use to discover DB IDs."""
    async with _client() as client:
        response = await client.post(
            "/search",
            json={"filter": {"value": "database", "property": "object"}, "page_size": page_size},
        )
        response.raise_for_status()
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


async def get_project_pages() -> dict[str, str]:
    """Project name → Notion page ID, for every top-level page the integration can see.

    Excludes pages that are rows in the Emails or Contacts database.
    """
    emails_id = settings.emails_db_id.replace("-", "")
    contacts_id = settings.contacts_db_id.replace("-", "")
    out: dict[str, str] = {}

    pages = await search_pages()
    for page in pages:
        parent = page.get("parent") or {}
        if parent.get("type") == "database_id":
            parent_db = (parent.get("database_id") or "").replace("-", "")
            if parent_db in (emails_id, contacts_id):
                continue
        title = extract_page_title(page)
        if title:
            out[title] = page["id"]
    return out


# ============================================================
# Emails DB
# ============================================================


async def find_email_row_by_message_id(message_id: str) -> dict[str, Any] | None:
    """Query the Emails DB for a non-archived row with this Gmail message ID."""
    if not settings.emails_db_id:
        raise RuntimeError("EMAILS_DB_ID is not configured")
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.emails_db_id}/query",
            json={
                "filter": {
                    "property": EMAILS_PROPS["message_id"],
                    "rich_text": {"equals": message_id},
                },
                "page_size": 5,
            },
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        live = [p for p in results if not p.get("archived") and not p.get("in_trash")]
        return live[0] if live else None


async def has_any_row_for_thread(thread_id: str) -> bool:
    """Quick existence check: any non-archived row with this Gmail thread ID."""
    if not settings.emails_db_id:
        raise RuntimeError("EMAILS_DB_ID is not configured")
    async with _client() as client:
        response = await client.post(
            f"/databases/{settings.emails_db_id}/query",
            json={
                "filter": {
                    "property": EMAILS_PROPS["thread_id"],
                    "rich_text": {"equals": thread_id},
                },
                "page_size": 1,
            },
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return any(not p.get("archived") and not p.get("in_trash") for p in results)


async def create_email_row(properties: dict[str, Any]) -> dict[str, Any]:
    """Create a new row in the Emails DB. Returns the created page object."""
    if not settings.emails_db_id:
        raise RuntimeError("EMAILS_DB_ID is not configured")
    async with _client() as client:
        response = await client.post(
            "/pages",
            json={
                "parent": {"database_id": settings.emails_db_id},
                "properties": properties,
            },
        )
        response.raise_for_status()
        return response.json()


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
        response.raise_for_status()
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
        response.raise_for_status()
        return response.json()


async def patch_contact(page_id: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Patch any subset of a contact's properties."""
    async with _client() as client:
        response = await client.patch(f"/pages/{page_id}", json={"properties": properties})
        response.raise_for_status()
        return response.json()


async def update_contact_phone(page_id: str, phone: str) -> None:
    """Convenience wrapper for the common "add phone if missing" case."""
    await patch_contact(page_id, {CONTACTS_PROPS["phone"]: {"phone_number": phone}})


# ============================================================
# Block helpers (for chat-style email row bodies)
# ============================================================


async def append_blocks_to_page(page_id: str, blocks: list[dict[str, Any]]) -> None:
    """Append a list of Notion blocks to a page. Notion caps each call at 100 children."""
    async with _client() as client:
        # Chunk into batches of 100 to respect Notion's limit.
        for i in range(0, len(blocks), 100):
            batch = blocks[i : i + 100]
            response = await client.patch(f"/blocks/{page_id}/children", json={"children": batch})
            response.raise_for_status()


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
