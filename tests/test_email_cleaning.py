"""Tests for utils/email_cleaning.py — covers English + Norwegian markers and edge cases."""

from gb_automations.utils.email_cleaning import (
    clean_body,
    extract_signature_block,
    find_attachment_reference_line,
    find_signature_start_line,
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
