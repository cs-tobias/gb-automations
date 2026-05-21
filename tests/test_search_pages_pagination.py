"""Regression test: search_pages/search_databases must paginate ALL /search results.

Pins the fix for the bug where a workspace with >100 visible objects (every
synced Email/Contact row counts toward Notion's 100-item /search cap) silently
dropped project pages past the first page. get_project_pages then couldn't match
a thread to its project, so sync_thread returned with no rows written and no log
— the "added a project, got nothing" symptom. The fix is a has_more/next_cursor
loop; these tests fail on the old single-shot implementation.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx

from gb_automations.clients import notion as notion_client


def _client_factory(total: int, page_size: int):
    """A _client() replacement whose /search returns `total` objects across
    multiple has_more responses of `page_size` each. The handler reads
    start_cursor from the request body, so it also verifies the loop sends the
    cursor on follow-up calls.
    """
    objects = [{"id": f"obj-{i}", "object": "page"} for i in range(total)]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        start = int(body.get("start_cursor") or 0)
        chunk = objects[start : start + page_size]
        next_start = start + page_size
        has_more = next_start < total
        return httpx.Response(
            200,
            json={
                "results": chunk,
                "has_more": has_more,
                "next_cursor": str(next_start) if has_more else None,
            },
        )

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.notion.com",
            transport=httpx.MockTransport(handler),
        )

    return factory


def test_search_pages_follows_has_more_across_multiple_pages():
    with patch.object(notion_client, "_client", _client_factory(total=250, page_size=100)):
        results = asyncio.run(notion_client.search_pages(page_size=100))
    assert len(results) == 250
    assert {r["id"] for r in results} == {f"obj-{i}" for i in range(250)}


def test_search_pages_single_page_when_no_has_more():
    with patch.object(notion_client, "_client", _client_factory(total=42, page_size=100)):
        results = asyncio.run(notion_client.search_pages())
    assert len(results) == 42


def test_search_databases_also_paginates():
    with patch.object(notion_client, "_client", _client_factory(total=130, page_size=100)):
        results = asyncio.run(notion_client.search_databases(page_size=100))
    assert len(results) == 130
