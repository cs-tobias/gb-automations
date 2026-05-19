"""Pins the "additive-only, everywhere" rule on contact writes.

The rule: when we encounter a contact that already exists in Notion (matched
by email or by exact-name fallback), we read each property's current value
and only write fields that are empty. We never overwrite a value Goldbox
put there — including Name and Email.

These tests cover the decision logic inside `patch_contact_enrichment`, the
function that all enrichment writes flow through.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

from gb_automations.clients import notion as notion_client
from gb_automations.config import CONTACTS_PROPS


def _empty_props() -> dict[str, Any]:
    """Notion returns properties as `{name: {type, <type-key>: <value>}}`.
    For 'empty' values, the type-key is present but None or empty list."""
    return {
        CONTACTS_PROPS["email"]: {"email": None},
        CONTACTS_PROPS["phone"]: {"phone_number": None},
        CONTACTS_PROPS["title"]: {"rich_text": []},
        CONTACTS_PROPS["address"]: {"rich_text": []},
        CONTACTS_PROPS["company"]: {"relation": []},
    }


def _filled_props() -> dict[str, Any]:
    return {
        CONTACTS_PROPS["email"]: {"email": "manual@example.com"},
        CONTACTS_PROPS["phone"]: {"phone_number": "+47 999 99 999"},
        CONTACTS_PROPS["title"]: {"rich_text": [{"plain_text": "Manager"}]},
        CONTACTS_PROPS["address"]: {"rich_text": [{"plain_text": "Karl Johans gate 1"}]},
        CONTACTS_PROPS["company"]: {"relation": [{"id": "company-page-1"}]},
    }


def _run(coro):
    return asyncio.run(coro)


def test_empty_row_gets_every_field_filled():
    """Manually-created row with just a Name → all five enrichment fields fill."""
    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=_empty_props(),
                email="new@example.com",
                phone="+47 111 11 111",
                title="Architect",
                address="Storgata 5",
                company_page_id="company-page-2",
            )
        )

    mock_patch.assert_awaited_once()
    written = mock_patch.await_args.args[1]
    assert written == {
        CONTACTS_PROPS["email"]: {"email": "new@example.com"},
        CONTACTS_PROPS["phone"]: {"phone_number": "+47 111 11 111"},
        CONTACTS_PROPS["title"]: {"rich_text": [{"text": {"content": "Architect"}}]},
        CONTACTS_PROPS["address"]: {"rich_text": [{"text": {"content": "Storgata 5"}}]},
        CONTACTS_PROPS["company"]: {"relation": [{"id": "company-page-2"}]},
    }


def test_fully_filled_row_writes_nothing():
    """Row has values everywhere → no patch issued at all."""
    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=_filled_props(),
                email="new@example.com",
                phone="+47 111 11 111",
                title="Architect",
                address="Storgata 5",
                company_page_id="company-page-2",
            )
        )

    mock_patch.assert_not_awaited()


def test_email_filled_phone_empty_only_phone_gets_written():
    """Mix: existing Email stays, blank Phone gets filled."""
    mixed = _empty_props()
    mixed[CONTACTS_PROPS["email"]] = {"email": "manual@example.com"}

    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=mixed,
                email="other@example.com",  # should NOT be written
                phone="+47 111 11 111",  # should be written
            )
        )

    mock_patch.assert_awaited_once()
    written = mock_patch.await_args.args[1]
    assert CONTACTS_PROPS["email"] not in written
    assert written[CONTACTS_PROPS["phone"]] == {"phone_number": "+47 111 11 111"}


def test_no_inputs_short_circuits_without_io():
    """If we have nothing to offer, we don't even GET the page."""
    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=_empty_props(),
                # no email/phone/title/address/company passed
            )
        )

    mock_patch.assert_not_awaited()


def test_email_field_only_filled_if_empty():
    """The core "additive even for Email" guarantee.

    When their row already has an Email, our incoming email is dropped on
    the floor — even if it differs. Their manual entry wins by virtue of
    being there first.
    """
    has_email = _empty_props()
    has_email[CONTACTS_PROPS["email"]] = {"email": "theirs@example.com"}

    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=has_email,
                email="ours@example.com",
            )
        )

    mock_patch.assert_not_awaited()


def test_relation_empty_list_counts_as_empty():
    """Notion sends `{"relation": []}` for an unset relation; that's empty."""
    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props={CONTACTS_PROPS["company"]: {"relation": []}},
                company_page_id="company-page-9",
            )
        )

    mock_patch.assert_awaited_once()
    written = mock_patch.await_args.args[1]
    assert written == {CONTACTS_PROPS["company"]: {"relation": [{"id": "company-page-9"}]}}


def test_relation_with_existing_entry_not_overwritten():
    has_company = _empty_props()
    has_company[CONTACTS_PROPS["company"]] = {"relation": [{"id": "existing-company"}]}

    with patch.object(notion_client, "patch_contact", new=AsyncMock()) as mock_patch:
        _run(
            notion_client.patch_contact_enrichment(
                "page-1",
                existing_props=has_company,
                company_page_id="different-company",
            )
        )

    mock_patch.assert_not_awaited()
