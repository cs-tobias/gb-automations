"""Thin async Notion REST client.

Just enough surface area for Stage 2 (search pages). Will grow as we port
the Apps Script logic in later stages.
"""

from typing import Any

import httpx

from gb_automations.config import settings

NOTION_API_BASE = "https://api.notion.com/v1"


def _headers() -> dict[str, str]:
    if not settings.notion_token:
        raise RuntimeError("NOTION_TOKEN is not configured")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": settings.notion_api_version,
        "Content-Type": "application/json",
    }


async def search_pages(page_size: int = 100) -> list[dict[str, Any]]:
    """Return every page the integration can see (top-level + DB rows)."""
    async with httpx.AsyncClient(base_url=NOTION_API_BASE, timeout=15.0) as client:
        response = await client.post(
            "/search",
            headers=_headers(),
            json={"filter": {"value": "page", "property": "object"}, "page_size": page_size},
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
