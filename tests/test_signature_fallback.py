"""Tests for the signature-parsing fallback chain.

Pins three things:
1. `body_before_quotes` trims at the first reply/forward marker without
   needing a sign-off line — this is what makes the structural fallback
   safe to run on raw bodies that lack "Mvh".
2. `parse_signature` recovers title from a Rino-style body (no sign-off,
   name → title → blank → phone) when given the pre-quote portion.
3. `classify_signature` parses Ollama output correctly and degrades to
   (None, None) on failure.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from gb_automations.clients import llm as llm_client
from gb_automations.utils.email_cleaning import body_before_quotes
from gb_automations.utils.phone import extract_phone
from gb_automations.utils.signature_parsing import parse_signature


# Rino's actual email body — captured from the failing log. No sign-off
# marker; signature region transitions straight from the attachment blurb
# to the name.
RINO_BODY = """\
Download full-resolution images Available until 17 Jun 2026

Click to Download
Aktiv SEO fra Klatre.pdf
1,4 MB
Click to Download
Aktiv SEO fra Klatre.pptx
18,4 MB

Rino Larsen
Daglig leder | Digital rådgiver

90 60 94 41
"""


# Same body but with a quoted reply tacked on. The reply contains a
# different person's signature, which the parser must NOT pick up when
# attributing fields to Rino.
RINO_BODY_WITH_REPLY = (
    RINO_BODY
    + """
fre. 16. mai 2026 kl. 09:12 skrev Petter Burhol <petter@goldbox.no>:

> Takk Rino!
>
> Petter Burhol
> Daglig leder
> petter@goldbox.no
"""
)


def test_body_before_quotes_keeps_full_body_when_no_reply_marker():
    assert body_before_quotes(RINO_BODY).strip() == RINO_BODY.strip()


def test_body_before_quotes_strips_quoted_reply():
    trimmed = body_before_quotes(RINO_BODY_WITH_REPLY)
    # Rino's own content survives
    assert "Rino Larsen" in trimmed
    assert "Daglig leder | Digital rådgiver" in trimmed
    # Petter's signature in the quoted reply is gone
    assert "petter@goldbox.no" not in trimmed
    assert "skrev Petter Burhol" not in trimmed


def test_parse_signature_recovers_title_from_rino_body_without_signoff():
    """The structural pass, applied to body_before_quotes(...), recovers
    Rino's title even though his email has no Mvh/Best Regards line."""
    source = body_before_quotes(RINO_BODY)
    fields = parse_signature(source, sender_name="Rino Larsen")
    assert fields.title == "Daglig leder | Digital rådgiver"


def test_extract_phone_recovers_norwegian_phone_from_body():
    """Layer 1's phone extraction runs against the same pre-quote body."""
    source = body_before_quotes(RINO_BODY)
    assert extract_phone(source) == "90 60 94 41"


def test_classify_signature_parses_good_json():
    """`classify_signature` returns the (title, phone) tuple when Ollama
    returns valid JSON with both fields populated."""
    fake_response = (
        '{"title": "Daglig leder | Digital rådgiver", "phone": "90 60 94 41"}',
        {"done": True},
    )
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        title, phone = asyncio.run(
            llm_client.classify_signature("body…", sender_name="Rino Larsen")
        )
    assert title == "Daglig leder | Digital rådgiver"
    assert phone == "90 60 94 41"


def test_classify_signature_handles_null_fields():
    """When Ollama legitimately reports no signature, both fields come back
    as None (the model returned JSON `null`)."""
    fake_response = ('{"title": null, "phone": null}', {"done": True})
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None)


def test_classify_signature_degrades_on_chat_failure():
    """A timeout or transport error must not raise — returns (None, None)
    so the contact row keeps whatever the regex layer found."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=TimeoutError("read timeout")),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None)


def test_classify_signature_degrades_on_malformed_json():
    fake_response = ("not json {{{", {"done": True})
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None)


def test_classify_signature_returns_none_when_body_is_empty():
    """No network call should be made for an empty body."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        result = asyncio.run(llm_client.classify_signature(""))
    assert result == (None, None)
