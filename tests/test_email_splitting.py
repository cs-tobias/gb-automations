"""Tests for utils/email_splitting.py — content-hashed synthetic IDs.

The historical LLM-driven `split_forwarded_chain` is gone; splitting moved to
`utils/history_extraction.py` (regex-based, deterministic). What remains here
is the `ExtractedMessage` dataclass and `synthetic_message_id`, which are
shared between the regex extractor and downstream sync code.
"""

from gb_automations.utils.email_splitting import synthetic_message_id

# ============================================================
# synthetic_message_id
# ============================================================


def test_synthetic_message_id_format():
    sid = synthetic_message_id("abc123", "Anne <a@x.com>", "Hei verden")
    assert sid.startswith("abc123#fwd-")
    # SHA-1 first 10 hex chars after the prefix.
    suffix = sid.removeprefix("abc123#fwd-")
    assert len(suffix) == 10
    assert all(c in "0123456789abcdef" for c in suffix)


def test_synthetic_message_id_separator_not_in_real_gmail_ids():
    sid = synthetic_message_id("anything", "from", "body")
    assert "#fwd-" in sid


def test_synthetic_message_id_is_deterministic_for_same_content():
    # Two calls with the same (from, body) → same ID. This is the property
    # that makes re-extractions idempotent.
    a = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden")
    b = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden")
    assert a == b


def test_synthetic_message_id_changes_with_body():
    a = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden")
    b = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden!")
    assert a != b


def test_synthetic_message_id_changes_with_from_field():
    a = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden")
    b = synthetic_message_id("parent1", "Bob <b@x.com>", "Hei verden")
    assert a != b


def test_synthetic_message_id_changes_with_parent():
    # Same content in two different parent messages → different IDs.
    # (The parent prefix scopes the hash to the Gmail message it came from.)
    a = synthetic_message_id("parent1", "Anne <a@x.com>", "Hei verden")
    b = synthetic_message_id("parent2", "Anne <a@x.com>", "Hei verden")
    assert a != b
