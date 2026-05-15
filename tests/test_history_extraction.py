"""Tests for utils/history_extraction.py — deterministic regex-based splitter.

Covers:
  - Header parsing across Gmail Norwegian/English, Apple Mail, Outlook, bare forms
  - Date parsing across Norwegian + English formats, AM/PM, missing year/time
  - End-to-end extraction on a realistic 18-message Norwegian thread
  - False-positive resistance (body prose that mentions dates/times)
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from gb_automations.utils.history_extraction import (
    _parse_human_date,
    extract_history_blocks,
    parse_header,
)

OSLO = timezone(timedelta(hours=1))
PARENT_DATE = datetime(2026, 5, 11, 11, 44, tzinfo=OSLO)


# ============================================================
# parse_header — single-line inline headers
# ============================================================


def test_parse_header_norwegian_gmail_with_kl():
    h = parse_header("fre. 8. mai 2026 kl. 13:55 skrev Hedda Torgersen <ht@metropolis.no>:")
    assert h.name == "Hedda Torgersen"
    assert h.email == "ht@metropolis.no"
    assert "8. mai 2026" in h.date_text
    assert "13:55" in h.date_text


def test_parse_header_norwegian_gmail_comma_time_no_kl():
    h = parse_header("man. 4. mai 2026, 15:43 skrev Hedda Torgersen <ht@metropolis.no>:")
    assert h.name == "Hedda Torgersen"
    assert h.email == "ht@metropolis.no"


def test_parse_header_norwegian_den_form():
    h = parse_header("Den 13. mai 2026 skrev Anne <a@x.com>:")
    assert h.name == "Anne"
    assert h.email == "a@x.com"


def test_parse_header_english_apple_mail():
    h = parse_header("On May 13, 2026, at 2:30 PM, John Doe <john@x.com> wrote:")
    assert h.name == "John Doe"
    assert h.email == "john@x.com"


def test_parse_header_english_bare_name_no_email():
    # Some clients omit <email>. Email field stays empty; name populated.
    h = parse_header("On May 11, 2026, Bob wrote:")
    assert h.name == "Bob"
    assert h.email == ""


def test_parse_header_norwegian_bare_name_no_email():
    h = parse_header("12. mai 2026 skrev Anne:")
    assert h.name == "Anne"
    assert h.email == ""


# ============================================================
# parse_header — multi-line Outlook-style forward header
# ============================================================


def test_parse_header_outlook_forward_block():
    text = (
        "---------- Forwarded message ---------\n"
        "From: Anne Doe <anne@example.com>\n"
        "Sent: Monday, May 13, 2026 2:30 PM\n"
        "To: Bob <bob@example.com>\n"
        "Subject: Quick question"
    )
    h = parse_header(text)
    assert h.name == "Anne Doe"
    assert h.email == "anne@example.com"
    assert h.subject == "Quick question"
    assert "May 13, 2026" in h.date_text


def test_parse_header_norwegian_forward_block():
    text = (
        "---------- Forwarded message ---------\n"
        "Fra: Silje Anett Bjørge <silje@goldbox.no>\n"
        "Date: fre. 8. mai 2026 kl. 13:58\n"
        "Subject: Re: OBOS-Versalen\n"
        "To: Hedda Torgersen <ht@metropolis.no>"
    )
    h = parse_header(text)
    assert h.name == "Silje Anett Bjørge"
    assert h.email == "silje@goldbox.no"
    assert h.subject == "Re: OBOS-Versalen"
    assert "8. mai 2026" in h.date_text


def test_parse_header_empty_returns_empty():
    h = parse_header("")
    assert h.name == "" and h.email == "" and h.date_text == "" and h.subject == ""


# ============================================================
# _parse_human_date
# ============================================================


def test_parse_date_norwegian_full():
    d = _parse_human_date("fre. 8. mai 2026 kl. 13:55", fallback=PARENT_DATE)
    assert d == datetime(2026, 5, 8, 13, 55, tzinfo=OSLO)


def test_parse_date_norwegian_no_kl():
    d = _parse_human_date("man. 4. mai 2026, 15:43", fallback=PARENT_DATE)
    assert d == datetime(2026, 5, 4, 15, 43, tzinfo=OSLO)


def test_parse_date_norwegian_abbreviated_month_with_period():
    d = _parse_human_date("tor. 30. apr. 2026 kl. 14:21", fallback=PARENT_DATE)
    assert d == datetime(2026, 4, 30, 14, 21, tzinfo=OSLO)


def test_parse_date_missing_year_uses_fallback_year():
    d = _parse_human_date("8. mai kl. 13:55", fallback=PARENT_DATE)
    assert d is not None
    assert d.year == PARENT_DATE.year
    assert d.month == 5
    assert d.day == 8


def test_parse_date_missing_time_defaults_to_midday():
    d = _parse_human_date("8. mai 2026", fallback=PARENT_DATE)
    assert d == datetime(2026, 5, 8, 12, 0, tzinfo=OSLO)


def test_parse_date_english_with_ampm_pm():
    d = _parse_human_date("May 13, 2026 at 2:30 PM", fallback=PARENT_DATE)
    assert d == datetime(2026, 5, 13, 14, 30, tzinfo=OSLO)


def test_parse_date_english_with_ampm_am():
    d = _parse_human_date("May 13, 2026, 9:15 AM", fallback=PARENT_DATE)
    assert d == datetime(2026, 5, 13, 9, 15, tzinfo=OSLO)


def test_parse_date_garbage_returns_none():
    assert _parse_human_date("not a date at all", fallback=PARENT_DATE) is None
    assert _parse_human_date("", fallback=PARENT_DATE) is None


# ============================================================
# extract_history_blocks — end-to-end on realistic thread
# ============================================================


_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "obos_versalen_thread.txt"


def _load_fixture() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


def test_extract_real_thread_finds_all_18_blocks():
    """The actual production case: an 18-message Norwegian thread that was
    losing messages under the LLM splitter. Regex extraction recovers them all.
    """
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    assert len(blocks) == 18


def test_extract_real_thread_chronological_order():
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    # Oldest first — each block's date should be <= the next.
    for prev, curr in zip(blocks, blocks[1:], strict=False):
        assert prev.date <= curr.date, f"out of order: {prev.date} > {curr.date}"


def test_extract_real_thread_first_block_is_heidi():
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    first = blocks[0]
    assert "Heidi T Bekkevold" in first.from_field
    assert "htb@metropolis.no" in first.from_field
    assert first.date == datetime(2026, 4, 30, 14, 21, tzinfo=OSLO)


def test_extract_real_thread_attribution_correct_for_short_replies():
    # Specific regression: "Tusen takk Heidi" was misattributed to Heidi by the
    # LLM; the regex parser names the speaker from the boundary line itself.
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    tusen_takk = next(b for b in blocks if b.body.startswith("Tusen takk"))
    assert "Silje" in tusen_takk.from_field


def test_extract_real_thread_handles_charlotte_with_signature():
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    charlotte = next(b for b in blocks if "Charlotte" in b.from_field)
    assert "ble mye bedre" in charlotte.body


# ============================================================
# extract_history_blocks — edge cases
# ============================================================


def test_extract_empty_body_returns_empty():
    assert extract_history_blocks("", "Subj", PARENT_DATE) == []


def test_extract_plain_message_with_no_quotes():
    body = "Hi, this is a plain message with no quoted history."
    assert extract_history_blocks(body, "Subj", PARENT_DATE) == []


def test_extract_single_reply():
    body = (
        "My response.\n\n"
        "On May 13, 2026, John <john@x.com> wrote:\n"
        "Original question here."
    )
    blocks = extract_history_blocks(body, "Re: Status", PARENT_DATE)
    assert len(blocks) == 1
    assert "John" in blocks[0].from_field
    assert "Original question" in blocks[0].body


def test_extract_does_not_trip_on_body_prose_mentioning_dates():
    # False-positive resistance: ordinary prose with times/dates shouldn't
    # trigger boundary detection. Only lines ending in "wrote:" or "skrev X:"
    # count as reply markers.
    body_cases = [
        "Hi, are you free Friday 02:23 pm? Let's meet.",
        "I have a 14:00 meeting tomorrow.\n\nBest, Anne",
        "Did you see the message Anne wrote on May 13? It mentioned the budget.",
        "On May 13, I'll be in Oslo, see you then.",
    ]
    for body in body_cases:
        assert extract_history_blocks(body, "Subj", PARENT_DATE) == [], (
            f"false positive on: {body!r}"
        )


def test_extract_english_chain_three_messages():
    body = (
        "My reply.\n\n"
        "On May 13, 2026, at 2:30 PM, John Doe <john@x.com> wrote:\n"
        "What's the status?\n\n"
        "On May 12, 2026, Anne <anne@x.com> wrote:\n"
        "Working on it.\n\n"
        "On May 11, 2026, Bob wrote:\n"
        "Initial question."
    )
    blocks = extract_history_blocks(body, "Re: Status", PARENT_DATE)
    assert len(blocks) == 3
    # Chronological: Bob (May 11) first, John (May 13) last.
    assert "Bob" in blocks[0].from_field
    assert "Anne" in blocks[1].from_field
    assert "John Doe" in blocks[2].from_field


def test_extract_handles_wrapped_email_in_reply_marker():
    # Regression: Gmail's plain-text renderer wraps at ~76 chars, sometimes
    # splitting `<email>` across two lines. Before the unwrap preprocessor,
    # the from-field came back as 'Heidi T Bekkevold <' and the email leaked
    # into the body.
    body = (
        "My response.\n\n"
        "tor. 30. apr. 2026 kl. 14:21 skrev Heidi T Bekkevold <\n"
        "htb@metropolis.no>:\n"
        "Hei, sender over en wetransfer.\n"
    )
    blocks = extract_history_blocks(body, "Re: Test", PARENT_DATE)
    assert len(blocks) == 1
    assert blocks[0].from_field == "Heidi T Bekkevold <htb@metropolis.no>"
    assert blocks[0].body == "Hei, sender over en wetransfer."


def test_extract_handles_wrapped_email_inside_deep_quote_levels():
    # Regression from real production data: in nested reply chains, the
    # wrapped <email> AND its continuation BOTH carry the same `>` quote
    # prefix. Naive unwrap embedded the quote prefix mid-line, corrupting
    # the email. The real Gmail body had this shape at quote level 16:
    body = (
        "Tusen takk Heidi ☺️\r\n"
        ">>>>>>>>>>>>>>>>\r\n"
        ">>>>>>>>>>>>>>>> tor. 30. apr. 2026 kl. 14:21 skrev Heidi T Bekkevold <\r\n"
        ">>>>>>>>>>>>>>>> htb@metropolis.no>:\r\n"
        ">>>>>>>>>>>>>>>>\r\n"
        ">>>>>>>>>>>>>>>>> Hei, sender over en wetransfer.\r\n"
    )
    blocks = extract_history_blocks(body, "Re: Test", PARENT_DATE)
    # Heidi's marker should produce one block with correct from + body.
    heidi = next((b for b in blocks if "Heidi T Bekkevold" in b.from_field), None)
    assert heidi is not None, f"no Heidi block found in {[b.from_field for b in blocks]}"
    assert heidi.from_field == "Heidi T Bekkevold <htb@metropolis.no>"
    assert "Hei, sender over en wetransfer." in heidi.body


def test_extract_handles_wrapped_cc_list_on_trailing_comma():
    # Regression from real production data: a `Cc:` header in a forward
    # block whose recipient list wrapped onto a second line via trailing
    # comma. The continuation has no `Cc:` prefix, so without explicit
    # join-on-comma the second line broke the boundary and bled the
    # recipient list into the block body.
    body = (
        "Mvh\r\nPetter\r\n\r\n"
        "---------- Forwarded message ---------\r\n"
        "Fra: Silje <silje@goldbox.no>\r\n"
        "Date: fre. 8. mai 2026 kl. 13:58\r\n"
        "Subject: Re: OBOS-Versalen\r\n"
        "To: Hedda <ht@metropolis.no>\r\n"
        "Cc: Charlotte <cha@metropolis.no>, Petter <petter@goldbox.no>,\r\n"
        "Annette <annette@goldbox.no>, Heidi <htb@metropolis.no>\r\n"
        "\r\n\r\n"
        "Så bra, takk!\r\n"
    )
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    assert len(blocks) == 1
    assert blocks[0].from_field == "Silje <silje@goldbox.no>"
    assert blocks[0].body == "Så bra, takk!"


def test_extract_handles_wrapped_email_with_inline_body():
    # Variant of the wrap case: after Gmail wraps the email, the body content
    # follows on the SAME line as the closing `>:`. Naive unwrap merged the
    # whole thing into one boundary, which then swallowed body into the name.
    # The split-after-terminator step in the unwrap preprocessor handles this.
    body = (
        "My response.\n\n"
        "tor. 30. apr. 2026 kl. 14:21 skrev Heidi T Bekkevold <\n"
        "htb@metropolis.no>: Hei, sender over en wetransfer met dokumenter.\n"
    )
    blocks = extract_history_blocks(body, "Re: Test", PARENT_DATE)
    assert len(blocks) == 1
    assert blocks[0].from_field == "Heidi T Bekkevold <htb@metropolis.no>"
    assert blocks[0].body == "Hei, sender over en wetransfer met dokumenter."


def test_extract_handles_wrapped_cc_list_in_forward_block():
    # Regression: a long Cc: list in a forward header gets wrapped to a second
    # line; the continuation line was breaking the boundary and leaking the
    # remaining recipients into the block body.
    body = (
        "Mvh\nPetter\n\n"
        "---------- Forwarded message ---------\n"
        "Fra: Silje <silje@goldbox.no>\n"
        "Date: fre. 8. mai 2026 kl. 13:58\n"
        "Subject: Re: OBOS-Versalen\n"
        "To: Hedda <ht@metropolis.no>\n"
        "Cc: Charlotte <cha@metropolis.no>, Petter <petter@goldbox.no>, Annette <\n"
        "annette@goldbox.no>, Heidi <htb@metropolis.no>\n\n"
        "Så bra, takk!\n"
    )
    blocks = extract_history_blocks(body, "Fwd: OBOS-Versalen", PARENT_DATE)
    assert len(blocks) == 1
    assert blocks[0].from_field == "Silje <silje@goldbox.no>"
    assert blocks[0].body == "Så bra, takk!"


def test_extract_outlook_style_single_forward():
    body = (
        "FYI.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Anne Doe <anne@x.com>\n"
        "Sent: Monday, May 13, 2026 2:30 PM\n"
        "To: Bob <bob@x.com>\n"
        "Subject: Quick question\n\n"
        "Hi Bob, could you check this?"
    )
    blocks = extract_history_blocks(body, "Fwd: Quick question", PARENT_DATE)
    assert len(blocks) == 1
    assert "Anne Doe" in blocks[0].from_field
    assert blocks[0].subject == "Quick question"
    assert "Hi Bob" in blocks[0].body
