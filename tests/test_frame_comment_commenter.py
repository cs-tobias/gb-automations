"""Tests for the Frame-comment → Notion commenter handling.

Covers the two pure-ish helpers in sync_frame_comments:
  - _comment_body: the Korreksjon row title is the CLEAN comment text, with
    NO "Author: " prefix (the old _bullet_text behavior is gone).
  - _resolve_commenter: find-or-create a Contacts page by email and return its
    id (the relation target). The contact holds the name + email; the row only
    gets the relation.
"""

from __future__ import annotations

import asyncio

import gb_automations.sync.sync_frame_comments as fc


def test_comment_body_is_clean_text_no_author_prefix():
    comment = {"text": "please darken the sky", "owner": {"name": "John Doe"}}
    assert fc._comment_body(comment) == "please darken the sky"


def test_comment_body_empty_falls_back():
    assert fc._comment_body({"text": ""}) == "(empty comment)"
    assert fc._comment_body({}) == "(empty comment)"


def _run(coro):
    return asyncio.run(coro)


def test_resolve_commenter_existing_contact(monkeypatch):
    monkeypatch.setattr(fc.settings, "contacts_db_id", "db123")

    async def fake_find(email):
        assert email == "john@x.com"
        return {"id": "contact-1"}

    created = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        return {"id": "should-not-be-used"}

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", fake_find)
    monkeypatch.setattr(fc.notion_client, "create_contact", fake_create)

    comment = {"owner": {"name": "John Doe", "email": "john@x.com"}}
    cid = _run(fc._resolve_commenter(comment))

    assert cid == "contact-1"
    assert created == []  # existing contact → no create


def test_resolve_commenter_creates_when_missing(monkeypatch):
    monkeypatch.setattr(fc.settings, "contacts_db_id", "db123")

    async def fake_find(email):
        return None

    created = []

    async def fake_create(**kwargs):
        created.append(kwargs)
        return {"id": "new-contact"}

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", fake_find)
    monkeypatch.setattr(fc.notion_client, "create_contact", fake_create)

    comment = {"owner": {"name": "Jane", "email": "jane@x.com"}}
    cid = _run(fc._resolve_commenter(comment))

    assert cid == "new-contact"
    assert created == [{"name": "Jane", "email": "jane@x.com"}]


def test_resolve_commenter_external_no_owner(monkeypatch):
    monkeypatch.setattr(fc.settings, "contacts_db_id", "db123")

    async def boom(*a, **k):  # must NOT be called without an email
        raise AssertionError("contact lookup should not run without an email")

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", boom)
    monkeypatch.setattr(fc.notion_client, "create_contact", boom)

    assert _run(fc._resolve_commenter({"owner": None})) is None


def test_resolve_commenter_name_only_no_email(monkeypatch):
    # Owner present with a name but no email → no relation (can't dedupe).
    monkeypatch.setattr(fc.settings, "contacts_db_id", "db123")

    async def boom(*a, **k):
        raise AssertionError("no email → no contact call")

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", boom)
    monkeypatch.setattr(fc.notion_client, "create_contact", boom)

    assert _run(fc._resolve_commenter({"owner": {"name": "Guest Reviewer"}})) is None


def test_resolve_commenter_no_contacts_db_configured(monkeypatch):
    # CONTACTS_DB_ID unset → no relation.
    monkeypatch.setattr(fc.settings, "contacts_db_id", "")

    async def boom(*a, **k):
        raise AssertionError("contacts disabled → no contact call")

    monkeypatch.setattr(fc.notion_client, "find_contact_by_email", boom)
    monkeypatch.setattr(fc.notion_client, "create_contact", boom)

    comment = {"owner": {"name": "John", "email": "john@x.com"}}
    assert _run(fc._resolve_commenter(comment)) is None
