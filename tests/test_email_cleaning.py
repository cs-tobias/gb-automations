"""Tests for utils/email_cleaning.py — covers English + Norwegian markers and edge cases."""

from gb_automations.utils.email_cleaning import (
    clean_body,
    count_reply_markers,
    extract_signature_block,
    find_attachment_reference_line,
    find_reply_boundaries,
    find_signature_start_line,
    has_quoted_history_hint,
    looks_like_forward_block,
)

# ============================================================
# clean_body
# ============================================================


def test_clean_body_returns_empty_for_empty_input():
    assert clean_body("") == ""
    assert clean_body("   \n  \n  ") == ""


def test_clean_body_keeps_simple_message():
    body = "Hi Tobias,\n\nThanks for the update.\n\nBest"
    # "Best" is not a signature marker on its own; "Best regards" is
    assert "Thanks for the update" in clean_body(body)


def test_clean_body_strips_english_reply_marker():
    body = (
        "Hi,\n\nMy reply text here.\n\n"
        "On May 13, 2026, John Doe <john@x.com> wrote:\n"
        "> Original message\n> goes here"
    )
    cleaned = clean_body(body)
    assert "My reply text here" in cleaned
    assert "Original message" not in cleaned


def test_clean_body_strips_norwegian_skrev_marker():
    body = (
        "Hei,\n\nDette er mitt svar.\n\n"
        "Den 13. mai 2026 skrev Petter Burhol <petter@x.com>:\n"
        "> Original tekst"
    )
    cleaned = clean_body(body)
    assert "Dette er mitt svar" in cleaned
    assert "Original tekst" not in cleaned


def test_clean_body_strips_norwegian_kl_skrev_marker():
    body = "Hei,\n\nMitt svar.\n\n13. mai 2026 kl. 14:30, skrev Anne <anne@x.com>:\n> quote"
    cleaned = clean_body(body)
    assert "Mitt svar" in cleaned
    assert "quote" not in cleaned


def test_clean_body_strips_forwarded_message_marker():
    body = "Forwarded for your review.\n\n---------- Forwarded message ---------\nFrom: A\nTo: B"
    cleaned = clean_body(body)
    assert "Forwarded for your review" in cleaned
    assert "From: A" not in cleaned


def test_clean_body_strips_norwegian_fra_header():
    body = "Hei,\n\nVidereformidler.\n\nFra: Anne <anne@x.com>\nSendt: 13. mai\nTil: tobias@..."
    cleaned = clean_body(body)
    assert "Videreformidler" in cleaned
    assert "anne@x.com" not in cleaned


def test_clean_body_strips_signature_mvh():
    body = "Hei,\n\nDette er innholdet.\n\nMvh\nTobias\nGoldbox"
    cleaned = clean_body(body)
    assert "Dette er innholdet" in cleaned
    assert "Tobias" not in cleaned
    assert "Goldbox" not in cleaned


def test_clean_body_strips_signature_med_vennlig_hilsen():
    body = "Hei,\n\nDette er innholdet.\n\nMed vennlig hilsen,\nTobias Eek"
    cleaned = clean_body(body)
    assert "Dette er innholdet" in cleaned
    assert "Tobias Eek" not in cleaned


def test_clean_body_strips_english_best_regards():
    body = "Hi,\n\nThe content.\n\nBest regards,\nJohn"
    cleaned = clean_body(body)
    assert "The content" in cleaned
    assert "John" not in cleaned


def test_clean_body_strips_dash_dash_signature_separator():
    body = "Real content.\n\n--\nJohn\nCEO, Acme"
    cleaned = clean_body(body)
    assert "Real content" in cleaned
    assert "Acme" not in cleaned


def test_clean_body_strips_sent_from_iphone():
    body = "Quick reply.\n\nSent from my iPhone"
    cleaned = clean_body(body)
    assert "Quick reply" in cleaned
    assert "iPhone" not in cleaned


def test_clean_body_strips_quote_prefix():
    body = "> Original quoted line\n> Another quoted line\nMy actual reply"
    cleaned = clean_body(body)
    # The quoted lines come first, but they don't trigger a reply marker, so they're
    # cleaned of the > prefix and kept. We're testing that > is stripped, not removed.
    assert ">" not in cleaned


def test_clean_body_strips_inline_image_markers():
    body = "Some text [image: logo.png] more text"
    cleaned = clean_body(body)
    assert "[image: logo.png]" not in cleaned
    assert "Some text" in cleaned
    assert "more text" in cleaned


def test_clean_body_strips_inline_url_brackets():
    body = "See here <https://example.com/path> for details"
    cleaned = clean_body(body)
    assert "<https://" not in cleaned
    assert "See here" in cleaned
    assert "for details" in cleaned


def test_clean_body_collapses_multiple_blank_lines_to_one():
    body = "Line 1\n\n\n\n\nLine 2"
    cleaned = clean_body(body)
    assert cleaned == "Line 1\n\nLine 2"


def test_clean_body_normalizes_double_space_to_newline():
    # Gmail sometimes squashes newlines into double-spaces; we restore them.
    body = "Line 1  Line 2  Line 3"
    cleaned = clean_body(body)
    assert "\n" in cleaned


def test_clean_body_handles_crlf_line_endings():
    body = "Line 1\r\nLine 2\r\n\r\nMvh\r\nTobias"
    cleaned = clean_body(body)
    assert "Line 1" in cleaned
    assert "Tobias" not in cleaned


def test_clean_body_returns_empty_for_pure_quote():
    # Message with nothing but a reply marker + quoted history → empty
    body = "On May 13 wrote:\n> quoted text\n> more"
    assert clean_body(body) == ""


# ============================================================
# extract_signature_block
# ============================================================


def test_extract_signature_block_finds_mvh():
    body = "Hei,\n\nInnholdet.\n\nMvh\nTobias Eek\nGoldbox\n+47 999 88 777"
    sig = extract_signature_block(body)
    assert "Mvh" in sig
    assert "Tobias Eek" in sig
    assert "999 88 777" in sig


def test_extract_signature_block_returns_empty_when_no_marker():
    body = "Just a message with no signature marker"
    assert extract_signature_block(body) == ""


def test_extract_signature_block_stops_at_reply_marker():
    # A "Mvh" inside a quoted forward should NOT be extracted
    body = "My new reply text.\n\nDen 13. mai skrev Anne:\n> Mvh\n> Anne\n"
    sig = extract_signature_block(body)
    assert sig == ""


def test_extract_signature_block_does_not_match_cheers_or_hilsen():
    # The block-extraction regex is stricter than the cleanBody markers
    body = "Some text.\n\nCheers,\nJohn"
    assert extract_signature_block(body) == ""


# ============================================================
# find_signature_start_line
# ============================================================


def test_find_signature_start_returns_minus_one_for_no_marker():
    assert find_signature_start_line("just text\nno markers") == -1


def test_find_signature_start_finds_last_signature_bottom_up():
    # If a forwarded segment higher up has a "Mvh", and the actual sender's
    # signature is at the bottom, we want the bottom one.
    body = (
        "My text\n"
        "Mvh\n"  # forwarded segment's signature
        "Anne\n"
        "\n"
        "Original message\n"
        "\n"
        "Mvh\n"  # actual current sender's signature  ← this is the one
        "Tobias\n"
    )
    idx = find_signature_start_line(body)
    lines = body.replace("\r\n", "\n").split("\n")
    # The result line should contain "Mvh" and be the LAST one in the body
    assert lines[idx].strip().startswith("Mvh")
    # And it should be near the end, not near the beginning
    assert idx > len(lines) // 2


def test_find_signature_start_handles_empty():
    assert find_signature_start_line("") == -1


# ============================================================
# find_attachment_reference_line
# ============================================================


def test_find_attachment_reference_finds_image_marker():
    body = "Top text\n[image: logo.png]\nMore text"
    assert find_attachment_reference_line(body, "logo.png") == 1


def test_find_attachment_reference_returns_minus_one_when_absent():
    body = "No image references here"
    assert find_attachment_reference_line(body, "logo.png") == -1


def test_find_attachment_reference_handles_special_chars_in_name():
    # File names with regex-special chars (like dots) must be escaped.
    body = "[image: image001.gif]"
    assert find_attachment_reference_line(body, "image001.gif") == 0


def test_find_attachment_reference_returns_minus_one_for_empty_inputs():
    assert find_attachment_reference_line("", "x.png") == -1
    assert find_attachment_reference_line("text", "") == -1


# ============================================================
# count_reply_markers
# ============================================================


def test_count_reply_markers_returns_zero_for_empty_and_plain():
    assert count_reply_markers("") == 0
    assert count_reply_markers("Just a normal email body with no markers.") == 0


def test_count_reply_markers_counts_one_for_normal_reply():
    # Single "On X wrote:" reply marker → not a forward chain.
    body = "My reply text.\n\nOn May 13, 2026, John wrote:\n> original"
    assert count_reply_markers(body) == 1


def test_count_reply_markers_counts_one_for_norwegian_skrev():
    body = "Mitt svar.\n\nDen 13. mai 2026 skrev Anne <a@x.com>:\n> sitat"
    assert count_reply_markers(body) == 1


def test_count_reply_markers_counts_multiple_for_forwarded_message():
    # A typical forward has the marker line plus a From: header → 2+ markers.
    body = (
        "FYI.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Anne <anne@x.com>\n"
        "Sent: May 12, 2026\n"
        "To: Bob <bob@x.com>\n"
        "Subject: Whatever\n\n"
        "Body of forwarded message."
    )
    assert count_reply_markers(body) >= 2


def test_count_reply_markers_counts_norwegian_forward_block():
    # Norwegian forward header block: Fra/Sendt/Til/Emne → several markers.
    body = (
        "Hei, ser dette.\n\n"
        "Fra: Anne <anne@x.com>\n"
        "Sendt: 12. mai 2026\n"
        "Til: tobias@goldbox.no\n"
        "Emne: Tilbud\n\n"
        "Hei, her er tilbudet."
    )
    assert count_reply_markers(body) >= 2


def test_count_reply_markers_counts_high_for_deep_chain():
    # Forward of a reply chain: at least 2 distinct header blocks.
    body = (
        "Topmost note.\n\n"
        "---------- Forwarded message ---------\n"
        "From: A <a@x.com>\n"
        "Sent: May 13\n\n"
        "Middle message.\n\n"
        "On May 12, 2026, B <b@x.com> wrote:\n"
        "> Original message"
    )
    assert count_reply_markers(body) >= 3


def test_count_reply_markers_handles_quoted_markers():
    # Even when markers are themselves quoted (>), they should count —
    # that's exactly the nested-forward scenario we want the LLM to handle.
    body = "Top.\n\n> From: Inner <i@x.com>\n> Sent: May 1\n> Subject: X"
    assert count_reply_markers(body) >= 2


# ============================================================
# looks_like_forward_block — tighter heuristic than count_reply_markers
# ============================================================


def test_looks_like_forward_block_false_for_empty_and_plain():
    assert looks_like_forward_block("") is False
    assert looks_like_forward_block("Just a normal email body with no markers.") is False


def test_looks_like_forward_block_false_for_normal_reply_with_on_wrote():
    # Normal English reply: "On X wrote:" is a reply marker, but there's no
    # forward header block, so we don't waste an LLM call.
    body = "My reply text.\n\nOn May 13, 2026, John wrote:\n> original line"
    assert looks_like_forward_block(body) is False


def test_looks_like_forward_block_false_for_norwegian_reply():
    body = "Mitt svar.\n\nDen 13. mai 2026 skrev Anne <a@x.com>:\n> sitat"
    assert looks_like_forward_block(body) is False


def test_looks_like_forward_block_false_for_single_quoted_from_line():
    # A reply that quotes ONE "From:" line should NOT trigger — that was
    # exactly the over-firing case the previous count-based heuristic had.
    body = "My reply.\n\nOn May 13 wrote:\n> From: Anne <a@x.com>\n> See attached."
    assert looks_like_forward_block(body) is False


def test_looks_like_forward_block_true_for_english_forwarded_marker():
    body = (
        "FYI.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Anne <anne@x.com>\n"
        "To: Bob <bob@x.com>\n\n"
        "Body of forwarded message."
    )
    assert looks_like_forward_block(body) is True


def test_looks_like_forward_block_true_for_norwegian_forwarded_marker():
    body = (
        "Se under.\n\n"
        "---------- Videresendt melding ---------\n"
        "Fra: Anne <anne@x.com>\n"
        "Til: Bob <bob@x.com>\n\n"
        "Innholdet."
    )
    assert looks_like_forward_block(body) is True


def test_looks_like_forward_block_true_for_norwegian_header_block_without_marker():
    # Some Gmail clients omit the "---- Forwarded message ----" line and
    # just emit a Fra/Sendt/Til/Emne block. Two consecutive header lines
    # is enough proof.
    body = (
        "Hei, ser dette.\n\n"
        "Fra: Anne <anne@x.com>\n"
        "Sendt: 12. mai 2026\n"
        "Til: tobias@goldbox.no\n"
        "Emne: Tilbud\n\n"
        "Innholdet."
    )
    assert looks_like_forward_block(body) is True


def test_looks_like_forward_block_true_for_english_header_block_without_marker():
    body = (
        "Hi, see below.\n\n"
        "From: Anne <anne@x.com>\n"
        "Sent: May 12, 2026\n"
        "To: tobias@goldbox.no\n"
        "Subject: Offer\n\n"
        "Body."
    )
    assert looks_like_forward_block(body) is True


def test_looks_like_forward_block_handles_quoted_forwards():
    # A deeply nested chain where the forward block is itself quoted with `>`.
    # The quote prefix is stripped before pattern matching.
    body = (
        "Top.\n\n"
        "> ---------- Forwarded message ---------\n"
        "> From: Inner <i@x.com>\n"
        "> To: x@y.com"
    )
    assert looks_like_forward_block(body) is True


def test_looks_like_forward_block_non_consecutive_headers_dont_trigger():
    # Two header-style lines separated by content lines (not a real block)
    # should NOT trigger.
    body = (
        "From: Someone, you know,\n"
        "I wanted to write because\n"
        "Sent: hopefully this works.\n"
    )
    assert looks_like_forward_block(body) is False


# ============================================================
# has_quoted_history_hint — cheap "should we try LLM reconstruction?" check
# ============================================================


def test_has_quoted_history_hint_false_for_empty_and_plain():
    assert has_quoted_history_hint("") is False
    assert has_quoted_history_hint("Just a normal email body with no markers.") is False


def test_has_quoted_history_hint_true_for_on_x_wrote():
    body = "My reply.\n\nOn May 13, 2026, John Doe <j@x.com> wrote:\n> original"
    assert has_quoted_history_hint(body) is True


def test_has_quoted_history_hint_true_for_norwegian_skrev():
    body = "Mitt svar.\n\nDen 13. mai 2026 skrev Anne <a@x.com>:\n> sitat"
    assert has_quoted_history_hint(body) is True


def test_has_quoted_history_hint_true_for_forwarded_marker():
    body = "FYI.\n\n---------- Forwarded message ---------\nFrom: Anne\n"
    assert has_quoted_history_hint(body) is True


def test_has_quoted_history_hint_true_for_norwegian_forward_marker():
    body = "Se under.\n\n---------- Videresendt melding ---------\nFra: Anne\n"
    assert has_quoted_history_hint(body) is True


def test_has_quoted_history_hint_true_for_single_from_header():
    # Unlike looks_like_forward_block which requires 2 consecutive headers,
    # has_quoted_history_hint fires on any single marker — including a single
    # `Fra:` line. That's fine because the cost of a false positive is just
    # one LLM call per thread (first encounter only), and the LLM will simply
    # return [] if there's nothing to extract.
    body = "Hei.\n\nFra: Anne <a@x.com>\n"
    assert has_quoted_history_hint(body) is True


def test_has_quoted_history_hint_handles_quoted_markers():
    # Markers behind `>` quote prefix still count.
    body = "Top.\n\n> On May 13 wrote:\n> > Original"
    assert has_quoted_history_hint(body) is True


# ============================================================
# find_reply_boundaries — counts distinct boundaries, not marker lines
# ============================================================


def test_find_reply_boundaries_empty():
    assert find_reply_boundaries("") == []


def test_find_reply_boundaries_no_markers():
    assert find_reply_boundaries("Just a plain email body, nothing forwarded.") == []


def test_find_reply_boundaries_single_reply():
    body = "My reply.\n\nOn May 13, 2026, John wrote:\n> original"
    bounds = find_reply_boundaries(body)
    assert len(bounds) == 1
    assert "John wrote" in bounds[0] or "wrote" in bounds[0].lower()


def test_find_reply_boundaries_header_block_counts_as_one():
    # A forward header with 4 consecutive header lines should be ONE boundary,
    # not four — they all describe the same forwarded message.
    body = (
        "FYI.\n\n"
        "---------- Forwarded message ---------\n"
        "From: Anne <anne@x.com>\n"
        "Sent: May 12, 2026\n"
        "To: Bob <bob@x.com>\n"
        "Subject: Hello\n\n"
        "Body content."
    )
    bounds = find_reply_boundaries(body)
    assert len(bounds) == 1


def test_find_reply_boundaries_nested_chain_counts_each():
    # Each "X skrev:" marker separated by a content line is its own boundary.
    body = (
        "Top reply.\n\n"
        "fre. 8. mai 2026 kl. 13:55 skrev Hedda <h@x.com>:\n"
        "Da er kommentarene lagt inn.\n\n"
        "fre. 8. mai 2026 kl. 09:05 skrev Silje <s@x.com>:\n"
        "Så bra!\n\n"
        "fre. 8. mai 2026 kl. 08:55 skrev Hedda <h@x.com>:\n"
        "Enig, dette ble bra."
    )
    bounds = find_reply_boundaries(body)
    assert len(bounds) == 3


def test_find_reply_boundaries_labels_are_truncated():
    # Reply markers require at least one digit (date-ish content) to avoid
    # false-positives on prose like "John wrote a long letter". A very long
    # but realistic date string still triggers and gets truncated to 120 chars.
    body = (
        "Top.\n\n"
        f"On May 13, 2026, at 2:30 PM, John {'a' * 300} <j@x.com> wrote:\n"
        "quote"
    )
    bounds = find_reply_boundaries(body)
    assert len(bounds) == 1
    # Truncated to 120 chars per the helper's contract.
    assert len(bounds[0]) <= 120
