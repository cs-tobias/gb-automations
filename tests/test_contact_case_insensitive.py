"""Email is lowercased before going out to Notion.

Pins the fix for the duplicate-Contact risk: Frame can emit the same address
with varying case across comments. If we ever pass that raw, Notion's `E-post`
property stores it verbatim and we end up with visually-distinct rows for the
same person. The lookup + create helpers normalize internally so any caller —
including a future one — is automatically safe.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx

from gb_automations.clients import notion as notion_client
from gb_automations.config import CONTACTS_PROPS
from gb_automations.sync import sync_frame_comments as fc


def _capture_client(captured: list[dict]) -> callable:
    """Return a _client() factory whose handler records the outbound request
    body and returns a no-results / empty-page response."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "method": request.method,
                "url": str(request.url),
                "body": json.loads(request.content or b"{}"),
            }
        )
        # find_contact_by_email → /databases/{id}/query: empty result list.
        # create_contact → /pages: a minimal page object echoing back.
        return httpx.Response(200, json={"id": "new-page", "results": []})

    def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url="https://api.notion.com",
            transport=httpx.MockTransport(handler),
        )

    return factory


def test_find_contact_by_email_lowercases_filter(monkeypatch):
    monkeypatch.setattr(notion_client.settings, "contacts_db_id", "db123")
    captured: list[dict] = []
    with patch.object(notion_client, "_client", _capture_client(captured)):
        result = asyncio.run(
            notion_client.find_contact_by_email("Tobias@Goldbox.NO")
        )
    assert result is None
    assert len(captured) == 1
    sent_email = captured[0]["body"]["filter"]["email"]["equals"]
    assert sent_email == "tobias@goldbox.no"


def test_create_contact_lowercases_email_property(monkeypatch):
    monkeypatch.setattr(notion_client.settings, "contacts_db_id", "db123")
    captured: list[dict] = []
    with patch.object(notion_client, "_client", _capture_client(captured)):
        asyncio.run(
            notion_client.create_contact(name="Tobias", email="Tobias@Goldbox.NO")
        )
    assert len(captured) == 1
    props = captured[0]["body"]["properties"]
    assert props[CONTACTS_PROPS["email"]]["email"] == "tobias@goldbox.no"


def test_resolve_commenter_lowercases_before_lookup(monkeypatch):
    """The Frame side normalizes at ingress too — belt and suspenders."""
    monkeypatch.setattr(fc.settings, "contacts_db_id", "db123")

    seen: list[str] = []

    async def fake_find(email):
        seen.append(email)
        return None

    created: list[dict] = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        return {"id": "new"}

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", fake_find)
    monkeypatch.setattr(fc.notion_client, "create_contact", fake_create)

    comment = {"owner": {"name": "Tobi", "email": "Tobias@Goldbox.NO"}}
    asyncio.run(fc._resolve_commenter(comment))

    assert seen == ["tobias@goldbox.no"]
    assert created[0]["email"] == "tobias@goldbox.no"
