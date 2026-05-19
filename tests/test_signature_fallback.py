"""Tests for the signature-detection chain.

Pins the contract for:
1. `body_before_quotes` — trims at the first reply/forward marker without
   needing a sign-off line. Bounds the LLM input safely.
2. `_signature_input` — applies trim → min-30 floor → tail-2000 cap to
   produce the exact string we feed `classify_signature`.
3. `_find_line_index` — strict line-equality match (not substring) used to
   resolve an LLM-supplied signature_first_line into a body line index.
4. `classify_signature` — 3-tuple return shape, schema parsing, graceful
   degradation on every failure mode.
5. `_partition_attachments` — when the regex sign-off marker is absent, the
   `signature_first_line_hint` correctly drops signature-region images.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.sync.sync_thread import (
    _find_line_index,
    _partition_attachments,
    _signature_input,
)
from gb_automations.utils.email_cleaning import body_before_quotes
from gb_automations.utils.phone import extract_phone


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

[image: PastedGraphic-7.tiff]
"""


# Same body but with a quoted reply tacked on. The reply contains a
# different person's signature, which trimming must drop.
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


# ============================================================
# body_before_quotes
# ============================================================


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


def test_extract_phone_recovers_norwegian_phone_from_body():
    """Regex phone backstop still works on the trimmed body — used to fill
    phone when the LLM call returns null for that field."""
    source = body_before_quotes(RINO_BODY)
    assert extract_phone(source) == "90 60 94 41"


# ============================================================
# _signature_input bounds
# ============================================================


def test_signature_input_returns_empty_for_short_body():
    """Bodies shorter than the min floor never contain a real signature.
    No need to spend an Ollama round-trip on them."""
    assert _signature_input("Hei!") == ""


def test_signature_input_caps_at_2000_chars_from_the_end():
    """Signatures sit at the end. A long pasted-document body returns its
    tail so the LLM prompt stays bounded."""
    long_body = "X" * 5000 + "\nRino Larsen\nDaglig leder\n"
    result = _signature_input(long_body)
    assert len(result) <= 2000
    # The signature lines at the end survive the cap
    assert "Rino Larsen" in result
    assert "Daglig leder" in result


def test_signature_input_trims_quoted_history_before_capping():
    """The trim runs FIRST, so even if the tail-2000 cap would have included
    quoted history, the marker-based trim drops it cleanly."""
    result = _signature_input(RINO_BODY_WITH_REPLY)
    # Quoted reply's content is gone
    assert "petter@goldbox.no" not in result
    # Rino's content survives
    assert "Rino Larsen" in result


# ============================================================
# _find_line_index
# ============================================================


def test_find_line_index_matches_exact_line():
    body = "Hello\nRino Larsen\nDaglig leder\n90 60 94 41\n"
    assert _find_line_index(body, "Rino Larsen") == 1
    assert _find_line_index(body, "Daglig leder") == 2


def test_find_line_index_strips_whitespace_on_both_sides():
    body = "Hello\n  Rino Larsen  \nDaglig leder\n"
    assert _find_line_index(body, "Rino Larsen") == 1


def test_find_line_index_does_not_match_substring():
    """A short needle like 'Hei' must not false-match a body line 'Hei Petter,'."""
    body = "Hei Petter,\nDet er en bra dag i dag.\nMvh\nPetter\n"
    assert _find_line_index(body, "Hei") == -1


def test_find_line_index_returns_negative_one_when_absent():
    assert _find_line_index("Hello\nWorld\n", "Goodbye") == -1


def test_find_line_index_handles_empty_inputs():
    assert _find_line_index("", "Rino Larsen") == -1
    assert _find_line_index("Hello\n", "") == -1
    assert _find_line_index("", "") == -1


# ============================================================
# classify_signature — 3-tuple contract
# ============================================================


def test_classify_signature_parses_good_json():
    """3-tuple return when Ollama returns all three fields populated."""
    fake_response = (
        '{"title": "Daglig leder | Digital rådgiver", '
        '"phone": "90 60 94 41", '
        '"signature_first_line": "Rino Larsen"}',
        {"done": True},
    )
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(
            llm_client.classify_signature("body…", sender_name="Rino Larsen")
        )
    assert result == (
        "Daglig leder | Digital rådgiver",
        "90 60 94 41",
        "Rino Larsen",
    )


def test_classify_signature_handles_null_fields():
    """When Ollama legitimately reports no signature, all three fields come
    back as None (the model returned JSON `null`)."""
    fake_response = (
        '{"title": null, "phone": null, "signature_first_line": null}',
        {"done": True},
    )
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None, None)


def test_classify_signature_degrades_on_chat_failure():
    """A timeout or transport error must not raise — returns (None, None, None)
    so the contact row keeps whatever the regex backstops found."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=TimeoutError("read timeout")),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None, None)


def test_classify_signature_degrades_on_malformed_json():
    fake_response = ("not json {{{", {"done": True})
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == (None, None, None)


def test_classify_signature_returns_none_when_body_is_empty():
    """No network call for an empty body."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        result = asyncio.run(llm_client.classify_signature(""))
    assert result == (None, None, None)


# ============================================================
# _partition_attachments — signature_first_line_hint wiring
# ============================================================


def _attachment(filename: str, *, mime: str = "image/tiff", size: int = 6500) -> gmail_client.GmailAttachment:
    return gmail_client.GmailAttachment(
        filename=filename,
        mime_type=mime,
        size=size,
        attachment_id="att-1",
        inline_ref_count=1,
    )


def test_partition_drops_signature_region_image_with_llm_hint():
    """Rino's body has no 'Mvh' sign-off marker — find_signature_start_line
    returns -1 — so today the TIFF would upload. With the LLM-derived hint
    ('Rino Larsen'), _partition_attachments locates the signature line and
    drops the TIFF as signature-region."""
    att = _attachment("PastedGraphic-7.tiff")
    decisions = _partition_attachments(
        RINO_BODY,
        [att],
        signature_first_line_hint="Rino Larsen",
    )
    assert len(decisions) == 1
    assert decisions[0].upload is False
    assert decisions[0].skip_reason == "signature-region"


def test_partition_without_hint_uploads_unmarked_signature_image():
    """Pre-fix behavior pinned: with no LLM hint and no regex marker, the
    TIFF uploads. This documents the gap the hint closes."""
    att = _attachment("PastedGraphic-7.tiff")
    decisions = _partition_attachments(RINO_BODY, [att])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_hint_with_no_matching_line_falls_back_safely():
    """An LLM hint that doesn't actually appear in the body must not crash
    or false-drop — we should fall back to today's regex-only behavior."""
    att = _attachment("PastedGraphic-7.tiff")
    decisions = _partition_attachments(
        RINO_BODY,
        [att],
        signature_first_line_hint="Some Person Who Isn't In This Email",
    )
    assert len(decisions) == 1
    assert decisions[0].upload is True
