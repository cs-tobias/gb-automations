"""Notion verification probes for the installer.

Notion has no public API for creating an internal integration, sharing
databases with it, or registering a webhook. So all this module does is:

  1. Verify that the token the operator pasted is valid (`GET /users/me`)
  2. Search for the three databases (Emails, Contacts, Project) — proves the
     integration was shared with each
  3. Return their IDs for writing into `.env`

Stdlib-only on purpose — the installer runs on the host outside the api
container's uv environment.
"""

from __future__ import annotations

import json
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

_API_BASE = "https://api.notion.com/v1"
_API_VERSION = "2022-06-28"  # Must match NOTION_API_VERSION in .env.example.


class NotionError(RuntimeError):
    pass


def _api(token: str, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib_request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Notion-Version": _API_VERSION,
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib_error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise NotionError(f"Notion API {method} {path} → HTTP {e.code}: {raw}") from e
    except urllib_error.URLError as e:
        raise NotionError(f"Notion API connection failed: {e}") from e
    return json.loads(raw)


def whoami(token: str) -> dict[str, Any]:
    """Probe the token. Returns the bot user record or raises."""
    return _api(token, "GET", "/users/me")


def search_databases(token: str) -> list[dict[str, Any]]:
    """Return every database the integration can see.

    Notion's `/search` is paginated; in practice an integration sees <100 DBs,
    so we don't bother with cursoring here.
    """
    resp = _api(
        token,
        "POST",
        "/search",
        body={"filter": {"property": "object", "value": "database"}, "page_size": 100},
    )
    return resp.get("results", [])


def find_db_id_by_title(databases: list[dict[str, Any]], wanted_titles: list[str]) -> str | None:
    """Match by title prefix (case-insensitive).

    `wanted_titles` lets the caller pass synonyms — e.g. ["Project", "Projects"]
    or ["Emails", "Emails 2026"] (year-partitioned DBs).
    """
    wanted_lower = [w.lower() for w in wanted_titles]
    for db in databases:
        title_parts = db.get("title", [])
        title = "".join(p.get("plain_text", "") for p in title_parts).strip().lower()
        if not title:
            continue
        if any(title == w or title.startswith(w) for w in wanted_lower):
            return db["id"]
    return None


def find_page_id_by_title(token: str, wanted_titles: list[str]) -> str | None:
    """Locate a top-level Notion *page* by title — used for EMAILS_PARENT_PAGE_ID.

    Year-partitioned Emails DBs live under a single parent page that the
    operator creates manually (we don't auto-create it, because Notion's API
    can't decide where in the workspace to put it).
    """
    resp = _api(
        token,
        "POST",
        "/search",
        body={"filter": {"property": "object", "value": "page"}, "page_size": 100},
    )
    wanted_lower = [w.lower() for w in wanted_titles]
    for page in resp.get("results", []):
        props = page.get("properties", {})
        # Pages have one "title"-typed property; its name varies ("title", "Name", etc.)
        title_text = ""
        for prop in props.values():
            if prop.get("type") == "title":
                title_text = "".join(p.get("plain_text", "") for p in prop.get("title", []))
                break
        if title_text and title_text.lower() in wanted_lower:
            return page["id"]
    return None
