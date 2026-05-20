"""Tests for the signature-detection chain.

Pins the contract for:
1. `body_before_quotes` — trims at the first reply/forward marker without
   needing a sign-off line. Bounds the LLM input safely.
2. `_signature_input` — applies trim → min-30 floor → tail-2000 cap to
   produce the exact string we feed `classify_signature`.
3. `_find_line_index` — strict line-equality match (not substring) used to
   resolve an LLM-supplied signature_first_line into a body line index.
4. `classify_signature` — `SignatureLocators` return shape, schema parsing,
   graceful degradation on every failure mode.
5. `_partition_attachments` — only sub-1KB tiny images are dropped; a normal
   image near the signature uploads (no position-based skip).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.clients.llm import SignatureLocators
from gb_automations.sync.sync_thread import (
    _find_line_index,
    _partition_attachments,
    _resolve_located_line,
    _signature_input,
    _slice_at_signature,
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
# classify_signature — SignatureLocators contract
# ============================================================


def test_classify_signature_parses_good_json():
    """SignatureLocators return when Ollama locates the lines. Address is NOT
    located by the LLM — only title/phone/signature_first_line."""
    fake_response = (
        '{"title_line": "Daglig leder | Digital rådgiver", '
        '"phone_line": "90 60 94 41", '
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
    assert result == SignatureLocators(
        title_line="Daglig leder | Digital rådgiver",
        phone_line="90 60 94 41",
        signature_first_line="Rino Larsen",
    )


def test_classify_signature_handles_no_signoff_name_line():
    """A signature with NO sign-off line (Signature A): signature_first_line is
    the sender's name, and the located lines keep their labels verbatim — the
    downstream cleaners strip the 'm:' label and the company suffix."""
    fake_response = (
        '{"title_line": "Daglig leder| NIMREM", '
        '"phone_line": "m: +47 93 88 32 01", '
        '"signature_first_line": "Caspar Vinje Hagland"}',
        {"done": True},
    )
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(
            llm_client.classify_signature("body…", sender_name="Caspar Vinje Hagland")
        )
    assert result == SignatureLocators(
        title_line="Daglig leder| NIMREM",
        phone_line="m: +47 93 88 32 01",
        signature_first_line="Caspar Vinje Hagland",
    )


def test_classify_signature_handles_null_fields():
    """When Ollama legitimately reports no signature, every field comes back
    as None (the model returned JSON `null`)."""
    fake_response = (
        '{"title_line": null, "phone_line": null, '
        '"signature_first_line": null}',
        {"done": True},
    )
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == SignatureLocators(None, None, None)


def test_classify_signature_degrades_on_chat_failure():
    """A timeout or transport error must not raise — returns all-None locators
    so the contact row keeps whatever the regex backstops found."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=TimeoutError("read timeout")),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == SignatureLocators(None, None, None)


def test_classify_signature_degrades_on_malformed_json():
    fake_response = ("not json {{{", {"done": True})
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(return_value=fake_response),
    ):
        result = asyncio.run(llm_client.classify_signature("body…"))
    assert result == SignatureLocators(None, None, None)


def test_classify_signature_returns_none_when_body_is_empty():
    """No network call for an empty body."""
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=AssertionError("should not be called")),
    ):
        result = asyncio.run(llm_client.classify_signature(""))
    assert result == SignatureLocators(None, None, None)


# ============================================================
# _resolve_located_line — verbatim → body line, with fallbacks
# ============================================================


def test_resolve_located_line_strict_match_returns_body_line():
    body = "Hei\nCaspar Vinje Hagland\nDaglig leder| NIMREM\n"
    assert _resolve_located_line(body, "Daglig leder| NIMREM") == "Daglig leder| NIMREM"


def test_resolve_located_line_substring_fallback_on_near_miss():
    """A near-miss (LLM dropped a trailing char) still resolves to the real
    body line via the case-insensitive substring fallback."""
    body = "Hei\nCaspar Vinje Hagland\nm: +47 93 88 32 01\n"
    # LLM returned the value without the trailing digit.
    assert _resolve_located_line(body, "m: +47 93 88 32 0") == "m: +47 93 88 32 01"


def test_resolve_located_line_falls_back_to_raw_when_absent():
    body = "Hei\nNoe helt annet\n"
    assert _resolve_located_line(body, "Senterleder") == "Senterleder"


def test_resolve_located_line_none_is_none():
    assert _resolve_located_line("a\nb\n", None) is None


# ============================================================
# _partition_attachments — no position-based signature skip
# ============================================================


def _attachment(filename: str, *, mime: str = "image/tiff", size: int = 6500) -> gmail_client.GmailAttachment:
    return gmail_client.GmailAttachment(
        filename=filename,
        mime_type=mime,
        size=size,
        attachment_id="att-1",
        inline_ref_count=1,
    )


def test_partition_uploads_image_in_signature_region():
    """The position-based signature-region skip was removed: an image whose
    marker sits in the signature region (and which isn't tiny or inline-
    repeated) now uploads. Losing a real photo was worse than letting a stray
    logo through; the cross-message repetition check still filters real
    signature logos in the upload loop."""
    att = _attachment("PastedGraphic-7.tiff")
    decisions = _partition_attachments([att])
    assert len(decisions) == 1
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


# ============================================================
# _slice_at_signature — drop signature from row body
# ============================================================


def test_slice_drops_signature_block_from_rino_body():
    """The whole signature region (name, title, blank, phone, image marker)
    disappears from the body. Everything above stays."""
    sliced = _slice_at_signature(RINO_BODY, "Rino Larsen")
    # Pre-signature content survives
    assert "Download full-resolution images" in sliced
    assert "Aktiv SEO fra Klatre.pdf" in sliced
    # Signature region is gone
    assert "Rino Larsen" not in sliced
    assert "Daglig leder | Digital rådgiver" not in sliced
    assert "90 60 94 41" not in sliced
    assert "PastedGraphic-7.tiff" not in sliced


def test_slice_is_noop_when_hint_is_none():
    assert _slice_at_signature(RINO_BODY, None) == RINO_BODY


def test_slice_is_noop_when_hint_does_not_match():
    """Hint not present in the body → body unchanged. Same safe-fallback
    pattern as _partition_attachments."""
    assert (
        _slice_at_signature(RINO_BODY, "Someone Not In This Email") == RINO_BODY
    )


def test_slice_handles_empty_body():
    assert _slice_at_signature("", "Rino Larsen") == ""


def test_slice_cuts_signature_mushed_onto_one_line():
    """When <span>-wrapped signature lines arrive mushed onto one line (no
    sign-off marker), the hint (the sender's name) still marks the cut point —
    everything from the name onward is dropped, the preceding text kept."""
    body = "Supert :)Rino LarsenDaglig leder | Digital rådgiver90 60 94 41"
    assert _slice_at_signature(body, "Rino Larsen") == "Supert :)"


def test_slice_cuts_embedded_signature_keeps_preceding_lines():
    body = "Takk!\nSnakkes.\nMvh Rino LarsenDaglig leder"
    sliced = _slice_at_signature(body, "Rino Larsen")
    assert sliced == "Takk!\nSnakkes.\nMvh"


def test_slice_drops_whole_line_when_signature_is_at_start():
    body = "Rino LarsenDaglig leder90 60 94 41"
    assert _slice_at_signature(body, "Rino Larsen") == ""
