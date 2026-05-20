"""Pins the name-match → email-conflict resolution in `_upsert_contact`.

The Notion contact Email field is single-valued. When a contact is matched by
exact name but the matched row already holds a *different* email, the incoming
address must get its OWN new row — silently dropping it (the old behavior) lost
real contacts. When the matched row's Email is empty (manual-row case) or equals
the incoming address, we reuse the row as before.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from gb_automations.clients import notion as notion_client
from gb_automations.config import CONTACTS_PROPS
from gb_automations.sync import sync_thread


def _run(coro):
    return asyncio.run(coro)


class _FakeSession:
    """Minimal AsyncSession stand-in: no cache hit, swallows the cache write."""

    async def get(self, *_args, **_kwargs):
        return None

    async def execute(self, *_args, **_kwargs):
        return None


def _contact(name: str, email: str) -> dict[str, Any]:
    return {"name": name, "email": email, "phone": None, "title": None, "address": None}


def _row_with_email(page_id: str, email: str | None) -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {CONTACTS_PROPS["email"]: {"email": email}},
    }


def test_name_match_with_different_email_creates_new_row():
    incoming = _contact("Tobias Eek", "hello@motionindex.io")
    matched = _row_with_email("existing-page", "post@tobiaseek.com")

    with (
        patch.object(notion_client, "find_contact_by_email", new=AsyncMock(return_value=None)),
        patch.object(
            notion_client, "find_contact_by_name_exact", new=AsyncMock(return_value=matched)
        ),
        patch.object(notion_client, "patch_contact_enrichment", new=AsyncMock()) as patch_enrich,
        patch.object(
            notion_client,
            "create_contact",
            new=AsyncMock(return_value={"id": "new-page"}),
        ) as create,
    ):
        page_id = _run(sync_thread._upsert_contact(incoming, None, _FakeSession()))

    assert page_id == "new-page"
    create.assert_awaited_once()
    # The matched row must NOT be touched — its existing email is preserved.
    patch_enrich.assert_not_awaited()


def test_name_match_with_empty_email_reuses_row():
    incoming = _contact("Tobias Eek", "hello@motionindex.io")
    matched = _row_with_email("existing-page", None)

    with (
        patch.object(notion_client, "find_contact_by_email", new=AsyncMock(return_value=None)),
        patch.object(
            notion_client, "find_contact_by_name_exact", new=AsyncMock(return_value=matched)
        ),
        patch.object(notion_client, "patch_contact_enrichment", new=AsyncMock()) as patch_enrich,
        patch.object(notion_client, "create_contact", new=AsyncMock()) as create,
    ):
        page_id = _run(sync_thread._upsert_contact(incoming, None, _FakeSession()))

    assert page_id == "existing-page"
    create.assert_not_awaited()
    # Reused row gets its empty Email filled additively.
    patch_enrich.assert_awaited_once()


def test_name_match_with_same_email_reuses_row():
    incoming = _contact("Tobias Eek", "post@tobiaseek.com")
    matched = _row_with_email("existing-page", "post@tobiaseek.com")

    with (
        patch.object(notion_client, "find_contact_by_email", new=AsyncMock(return_value=None)),
        patch.object(
            notion_client, "find_contact_by_name_exact", new=AsyncMock(return_value=matched)
        ),
        patch.object(notion_client, "patch_contact_enrichment", new=AsyncMock()),
        patch.object(notion_client, "create_contact", new=AsyncMock()) as create,
    ):
        page_id = _run(sync_thread._upsert_contact(incoming, None, _FakeSession()))

    assert page_id == "existing-page"
    create.assert_not_awaited()
