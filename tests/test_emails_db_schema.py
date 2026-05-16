"""Tests for the year-partitioned Emails DB plumbing.

The async paths (Notion search/create, Postgres cache) are exercised by
live verification — these unit tests pin the schema builder and the
ID-comparison helper, which are the pure logic worth locking down.
"""

from __future__ import annotations

import pytest

from gb_automations.clients.notion_emails_db import (
    _emails_db_title,
    _normalize_id,
    is_known_emails_db_id,
)
from gb_automations.config import (
    EMAIL_TAGS,
    EMAILS_PROPS,
    build_emails_db_schema,
)
from gb_automations.sync.sync_thread import _emails_from


def _build(
    *,
    projects_db_id: str = "proj",
    contacts_db_id: str = "contacts",
    tag_options: list[str] | None = None,
) -> dict[str, dict]:
    """Test helper — most tests don't care about IDs/tags, default them."""
    return build_emails_db_schema(
        projects_db_id=projects_db_id,
        contacts_db_id=contacts_db_id,
        tag_options=tag_options if tag_options is not None else EMAIL_TAGS,
    )


def test_emails_db_title_is_year_prefixed():
    assert _emails_db_title(2024) == "Emails 2024"
    assert _emails_db_title(2030) == "Emails 2030"


def test_normalize_id_strips_dashes_and_lowercases():
    assert _normalize_id("ABC-123-def") == "abc123def"
    assert _normalize_id("abc123def") == "abc123def"


def test_is_known_emails_db_id_matches_dash_variants():
    known = {"abc123def"}
    assert is_known_emails_db_id("abc-123-def", known) is True
    assert is_known_emails_db_id("ABC123DEF", known) is True
    assert is_known_emails_db_id("abc-123-DEF", known) is True
    assert is_known_emails_db_id("different", known) is False


def test_build_schema_requires_projects_db_id():
    with pytest.raises(ValueError, match="projects_db_id is required"):
        build_emails_db_schema(
            projects_db_id="", contacts_db_id="c", tag_options=["a"]
        )


def test_build_schema_requires_contacts_db_id():
    with pytest.raises(ValueError, match="contacts_db_id is required"):
        build_emails_db_schema(
            projects_db_id="p", contacts_db_id="", tag_options=["a"]
        )


def test_build_schema_covers_every_emails_props_name():
    """Every name in EMAILS_PROPS must have a corresponding entry in the
    schema. If we add a new property to EMAILS_PROPS without updating
    `build_emails_db_schema`, year DBs would be created missing that
    column and the row builder would silently skip writes to it."""
    schema = _build()
    for name in EMAILS_PROPS.values():
        assert name in schema, f"missing schema entry for {name!r}"


def test_build_schema_project_is_relation_to_projects_db():
    schema = _build(projects_db_id="proj_db_id")
    project_entry = schema[EMAILS_PROPS["project"]]
    assert "relation" in project_entry
    assert project_entry["relation"]["database_id"] == "proj_db_id"


def test_build_schema_from_to_cc_are_relations_to_contacts_db():
    schema = _build(contacts_db_id="contacts_db_id")
    for prop_key in ("from_contact", "to_contacts", "cc_contacts"):
        entry = schema[EMAILS_PROPS[prop_key]]
        assert "relation" in entry, f"{prop_key} should be a relation"
        assert entry["relation"]["database_id"] == "contacts_db_id"


def test_build_schema_seeds_tag_options():
    schema = _build(tag_options=["tilbud", "bestilling", "korreksjon"])
    tags_entry = schema[EMAILS_PROPS["tags"]]
    option_names = [opt["name"] for opt in tags_entry["multi_select"]["options"]]
    assert option_names == ["tilbud", "bestilling", "korreksjon"]


def test_build_schema_title_property_is_subject():
    """Notion requires exactly one `title` property per DB; ours is Subject."""
    schema = _build()
    title_props = [name for name, defn in schema.items() if "title" in defn]
    assert title_props == [EMAILS_PROPS["subject"]]


def test_build_schema_property_order_matches_notion_layout():
    """Notion creates DB columns left-to-right in the order properties appear
    in the create request. We pin a specific order so users see body and
    participants first, then date/files, then categorical fields, then the
    two technical ID columns at the very end."""
    schema = _build()
    expected_order = [
        EMAILS_PROPS["subject"],       # title — Notion requires first
        EMAILS_PROPS["body"],
        EMAILS_PROPS["from_contact"],
        EMAILS_PROPS["to_contacts"],
        EMAILS_PROPS["cc_contacts"],
        EMAILS_PROPS["date"],
        EMAILS_PROPS["files"],
        EMAILS_PROPS["tags"],
        EMAILS_PROPS["project"],
        EMAILS_PROPS["thread_id"],
        EMAILS_PROPS["message_id"],
    ]
    assert list(schema.keys()) == expected_order


def test_build_schema_no_legacy_direction_or_from_email():
    """The relation refactor replaced `From Email` + `Direction` with
    Contact relations. Make sure the old names don't sneak back."""
    schema = _build()
    assert "From Email" not in schema
    assert "Direction" not in schema


# ============================================================
# _emails_from — comma-split + parse + dedupe
# ============================================================


def test_emails_from_parses_multiple_recipients():
    field = "Alice <alice@x.com>, Bob <bob@x.com>, charlie@x.com"
    assert _emails_from(field) == ["alice@x.com", "bob@x.com", "charlie@x.com"]


def test_emails_from_dedupes_preserving_order():
    field = "Alice <alice@x.com>, alice@x.com, Bob <bob@x.com>"
    assert _emails_from(field) == ["alice@x.com", "bob@x.com"]


def test_emails_from_skips_name_only_entries():
    """A `To:` header carrying just a display name (no `<email>`) can't be
    related to a Contact — skip silently rather than create a no-email
    contact for a possibly-misparsed value."""
    field = "Anonymous Sender, real@example.com"
    assert _emails_from(field) == ["real@example.com"]


def test_emails_from_empty_returns_empty():
    assert _emails_from("") == []
    assert _emails_from("   ") == []
