"""Signature enrichment runs on forwarded-thread history, not just live messages.

A sender whose signature only appears inside the forwarded history (their live
message being a signature-only forward) must still get title/phone/address.
`_enrich_sender_from_body` is the shared pipeline; it stops per-sender once all
three fields are filled (success-based dedup, not attempt-based).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from gb_automations.clients import llm as llm_client
from gb_automations.clients.llm import SignatureLocators
from gb_automations.sync import sync_thread

# Hedda's real signature, as it sits inside a forwarded history segment's
# raw_body. The regex backstop alone recovers every field, so these tests pin
# the history-feeds-pipeline wiring without needing a live LLM.
HEDDA_HISTORY_BODY = """\
Hei,

Da er kommentarene på bilde 28 - inngangsparti utendørs lagt inn :)

Ønsker dere en fin helg i sola!

Med vennlig hilsen

HEDDA TORGERSEN

Interiørarkitekt MA

+47 93 48 44 35

ht@metropolis.no

metropolis.no

Metropolis arkitektur & design as

Rosenborggata 19C

0356 Oslo/Norway
"""


def _record(name: str) -> dict:
    return {
        "name": name,
        "email": "ht@metropolis.no",
        "phone": None,
        "title": None,
        "address": None,
    }


def _run(coro):
    return asyncio.run(coro)


def test_enrich_recovers_full_signature_from_history_body():
    """The real path: the LLM locates the lines in the history segment, the
    cleaners produce the values. This is what fills Hedda's contact from a
    forwarded thread where her signature only appears in history."""
    rec = _record("Hedda Torgersen")
    response = (
        '{"title_line": "Interiørarkitekt MA", '
        '"phone_line": "+47 93 48 44 35", '
        '"signature_first_line": "Med vennlig hilsen"}',
        {"done": True},
    )
    with patch.object(
        llm_client, "_chat_streaming", new=AsyncMock(return_value=response)
    ):
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="ht@metropolis.no",
                body=HEDDA_HISTORY_BODY,
                sender_signature_lines={},
            )
        )
    assert rec["title"] == "Interiørarkitekt MA"
    assert rec["phone"] == "+47 93 48 44 35"
    # Address comes from the regex backstop (street + postal joined), not the LLM.
    assert rec["address"] == "Rosenborggata 19C, 0356 Oslo/Norway"


def test_enrich_backstop_fills_phone_and_address_but_not_title():
    """When the LLM returns nothing, the regex backstop recovers phone and
    address. Title is intentionally LLM-only (parse_signature's title is too
    brittle), so it stays empty here."""
    rec = _record("Hedda Torgersen")
    null_response = (
        '{"title_line": null, "phone_line": null, '
        '"signature_first_line": null}',
        {"done": True},
    )
    with patch.object(
        llm_client, "_chat_streaming", new=AsyncMock(return_value=null_response)
    ):
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="ht@metropolis.no",
                body=HEDDA_HISTORY_BODY,
                sender_signature_lines={},
            )
        )
    assert rec["phone"] == "+47 93 48 44 35"
    assert rec["address"] == "Rosenborggata 19C, 0356 Oslo/Norway"
    assert rec["title"] is None


def test_enrich_skips_llm_when_already_complete():
    """Success-based dedup: a record with all three fields set never calls the
    LLM again, no matter how many more of the sender's messages we feed."""
    rec = _record("Hedda Torgersen")
    rec.update(title="X", phone="Y", address="Z")
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=AssertionError("classify_signature must not run")),
    ):
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="ht@metropolis.no",
                body=HEDDA_HISTORY_BODY,
                sender_signature_lines={},
            )
        )
    assert (rec["title"], rec["phone"], rec["address"]) == ("X", "Y", "Z")


def test_enrich_short_body_skips_llm_but_record_unchanged():
    """A stub forward under the min-char floor skips the LLM (no wasted call)
    and leaves the record empty — recovery happens on the substantive segment."""
    rec = _record("Hedda Torgersen")
    with patch.object(
        llm_client,
        "_chat_streaming",
        new=AsyncMock(side_effect=AssertionError("classify_signature must not run")),
    ):
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="ht@metropolis.no",
                body="forwarded message",
                sender_signature_lines={},
            )
        )
    assert (rec["title"], rec["phone"], rec["address"]) == (None, None, None)


def test_enrich_llm_locators_win_then_backstop_fills_gaps():
    """LLM locates title+phone; address is left to the regex backstop."""
    rec = _record("Hedda Torgersen")
    loc = SignatureLocators(
        title_line="Interiørarkitekt MA",
        phone_line="+47 93 48 44 35",
        signature_first_line="Med vennlig hilsen",
    )
    with patch.object(
        llm_client, "classify_signature", new=AsyncMock(return_value=loc)
    ):
        sig_lines: dict[str, str] = {}
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="ht@metropolis.no",
                body=HEDDA_HISTORY_BODY,
                sender_signature_lines=sig_lines,
            )
        )
    assert rec["title"] == "Interiørarkitekt MA"
    assert rec["phone"] == "+47 93 48 44 35"
    assert rec["address"] == "Rosenborggata 19C, 0356 Oslo/Norway"  # backstop
    assert sig_lines["ht@metropolis.no"] == "Med vennlig hilsen"


def test_enrich_name_only_signature_does_not_set_name_as_title():
    """Charlotte's signature is just name + phone — no title line. Even if the
    LLM points title_line at the name, the cleaner rejects it (name guard), so
    she gets a phone but no title and no address."""
    rec = {
        "name": "Charlotte Hagemoen",
        "email": "cha@metropolis.no",
        "phone": None,
        "title": None,
        "address": None,
    }
    body = "Umiddelbart synes jeg dette ble bedre.\nCharlotte Hagemoen\n+4741682942\n"
    # The model wrongly returns the name as the title; phone is correct.
    loc = SignatureLocators(
        title_line="Charlotte Hagemoen",
        phone_line="+4741682942",
        signature_first_line="Charlotte Hagemoen",
    )
    with patch.object(llm_client, "classify_signature", new=AsyncMock(return_value=loc)):
        _run(
            sync_thread._enrich_sender_from_body(
                rec,
                sender_email="cha@metropolis.no",
                body=body,
                sender_signature_lines={},
            )
        )
    assert rec["title"] is None
    assert rec["phone"] == "+4741682942"
    assert rec["address"] is None
