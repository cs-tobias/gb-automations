"""Regression tests for the Outlook splitter fixes.

This thread (`19e2b76ceaf9facd` in production, captured as
fixtures/outlook_thon_thread.txt) is a Norwegian forward originating from
Outlook. Gmail's plain-text rendering wraps Outlook's bolded header labels
as markdown bold:

    *Fra:* Petter Burhol <petter@goldbox.no>
    *Sendt:* torsdag 20. april 2023 14:57
    *Til:* Ingeborg Kvamme Skar <ingeborg.skar@olavthon.no>
    *Kopi:* Irene Vibeke Johnsen <irene.johnsen@olavthon.no>
    *Emne:* Re: Re: Digital styling - tilbud Goldbox

The pre-fix regex required `^Fra:` at line-start; the leading `*` defeated
it, so the splitter missed two Outlook boundaries and glued unrelated
messages together. These tests pin the asterisk-tolerant fix in place and
also cover the `find_under_split_blocks` validator that drives the LLM
fallback when regex still misses a format.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from gb_automations.utils.email_cleaning import find_reply_boundary_ranges
from gb_automations.utils.email_splitting import (
    ExtractedMessage,
    find_under_split_blocks,
)
from gb_automations.utils.history_extraction import (
    extract_history_blocks,
    parse_header,
)

_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "outlook_thon_thread.txt"


def _load_fixture() -> str:
    return _FIXTURE_PATH.read_text(encoding="utf-8")


# Parent date: the outer forwarder's message arrived in May 2026. Used as
# the fallback date when an extracted block's own date can't be parsed.
PARENT_DATE = datetime(2026, 5, 15, 11, 47, tzinfo=UTC)


# ============================================================
# Asterisk-wrapped Outlook header — boundary detection
# ============================================================


def test_asterisk_fra_opens_boundary():
    """A line `*Fra:* Name <email>` must open a forward-header boundary the
    same way a plain `Fra:` line does."""
    body = (
        "Hei, et lite svar.\n\n"
        "*Fra:* Petter Burhol <petter@goldbox.no>\n"
        "*Sendt:* torsdag 20. april 2023 14:57\n"
        "*Til:* Ingeborg <ingeborg@example.com>\n"
        "*Emne:* Re: Re: Digital styling\n\n"
        "Hmm, det var litt mye forskjell på prisene.\n"
    )
    boundaries = find_reply_boundary_ranges(body)
    assert len(boundaries) == 1
    assert boundaries[0].label.startswith("*Fra:*")


def test_outlook_header_block_with_blank_gaps_is_one_boundary():
    """Outlook's HTML renders each header label in its own `<p>`. After HTML
    stripping, that becomes:

        Fra: Petter <petter@x.com>
        (blank)
        Sendt: ...
        (blank)
        Til: ...

    The boundary scanner must span those blank gaps when the next non-blank
    line is another forward-header-extension. Otherwise each label becomes
    its own boundary and the body lands on the wrong row."""
    body = (
        "Some prose above.\n\n"
        "Fra: Petter Burhol <petter@goldbox.no>\n\n"
        "Sendt: torsdag 20. april 2023 14:57\n\n"
        "Til: Ingeborg <ingeborg@example.com>\n\n"
        "Kopi: Irene <irene@example.com>\n\n"
        "Emne: Re: noe\n\n"
        "The actual body content of this message.\n"
    )
    boundaries = find_reply_boundary_ranges(body)
    assert len(boundaries) == 1
    # All five header fields should be in the single boundary's header_text.
    headers = boundaries[0].header_text
    assert "Fra:" in headers
    assert "Sendt:" in headers
    assert "Til:" in headers
    assert "Kopi:" in headers
    assert "Emne:" in headers


def test_blank_gap_followed_by_prose_still_closes_boundary():
    """If the next non-blank line is NOT a header-extension, the blank still
    closes the boundary. Just an inline `Fra:` in a regular reply (rare but
    possible) shouldn't keep eating prose below."""
    body = (
        "Fra: Petter <petter@goldbox.no>\n\n"
        "This is just regular prose that follows.\n"
    )
    boundaries = find_reply_boundary_ranges(body)
    assert len(boundaries) == 1
    # The boundary should end at the line with `Fra:`, not include the prose.
    assert "prose" not in boundaries[0].header_text


def test_asterisk_outlook_kopi_extends_boundary():
    """`*Kopi:*` (Norwegian Outlook Cc) should extend an open boundary."""
    body = (
        "*Fra:* A <a@x.com>\n"
        "*Sendt:* mandag 1. mai 2023 09:00\n"
        "*Til:* B <b@x.com>\n"
        "*Kopi:* C <c@x.com>\n"
        "*Emne:* Re: noe\n\n"
        "Selve innholdet.\n"
    )
    boundaries = find_reply_boundary_ranges(body)
    assert len(boundaries) == 1
    # All five header lines belong to the same boundary.
    assert "Kopi" in boundaries[0].header_text
    assert "Emne" in boundaries[0].header_text


def test_parse_header_strips_asterisks_from_outlook_fields():
    header = (
        "*Fra:* Petter Burhol <petter@goldbox.no>\n"
        "*Sendt:* torsdag 20. april 2023 14:57\n"
        "*Emne:* Re: Re: Digital styling - tilbud Goldbox"
    )
    parsed = parse_header(header)
    assert parsed.email == "petter@goldbox.no"
    assert "Petter" in parsed.name
    assert "20. april 2023" in parsed.date_text
    assert "Digital styling" in parsed.subject


def test_parse_header_extracts_to_and_cc_fields():
    """Outlook forward headers carry `Til:` (To) and `Kopi:` (Cc) lines. The
    relation refactor needs these so each historical row gets To/Cc relations
    on its Notion page, not just the outer-message row."""
    header = (
        "*Fra:* Petter Burhol <petter@goldbox.no>\n"
        "*Sendt:* torsdag 20. april 2023 14:57\n"
        "*Til:* Ingeborg <ingeborg@example.com>\n"
        "*Kopi:* Irene <irene@example.com>, Anne <anne@example.com>\n"
        "*Emne:* Re: noe"
    )
    parsed = parse_header(header)
    assert "ingeborg@example.com" in parsed.to_text
    assert "irene@example.com" in parsed.cc_text
    assert "anne@example.com" in parsed.cc_text


def test_parse_header_to_and_cc_default_to_empty_when_absent():
    """English Gmail-style inline reply headers don't carry To/Cc — those
    fields stay empty strings."""
    header = "ons. 19. apr. 2023 kl. 12:11 skrev Ingeborg <i@x.com>:"
    parsed = parse_header(header)
    assert parsed.to_text == ""
    assert parsed.cc_text == ""


# ============================================================
# Real-thread fixture: pin the boundary count + chronology
# ============================================================


def test_real_outlook_thread_finds_five_boundaries():
    """Fixture from production thread 19e2b76ceaf9facd — has 5 boundaries:
    one Gmail-style forward divider, two Outlook `*Fra:*` blocks, and two
    Norwegian `skrev` inline replies. The pre-fix splitter found only 3
    (it missed both Outlook blocks)."""
    body = _load_fixture()
    boundaries = find_reply_boundary_ranges(body)
    assert len(boundaries) == 5


def test_real_outlook_thread_extracts_five_chronological_blocks():
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: Re: Digital styling - tilbud Goldbox", PARENT_DATE)
    assert len(blocks) == 5
    # Oldest first.
    for prev, curr in zip(blocks, blocks[1:], strict=False):
        assert prev.date <= curr.date


def test_real_outlook_thread_includes_petter_messages():
    """The pre-fix bug glued Petter's two messages onto Ingeborg's blocks. After
    the fix, at least one extracted block should be authored by Petter."""
    body = _load_fixture()
    blocks = extract_history_blocks(body, "Fwd: Re: Digital styling - tilbud Goldbox", PARENT_DATE)
    senders = {b.from_field.lower() for b in blocks}
    assert any("petter" in s for s in senders), f"Petter not found in: {senders}"
    assert any("ingeborg" in s for s in senders), f"Ingeborg not found in: {senders}"


# ============================================================
# find_under_split_blocks — drives the LLM fallback
# ============================================================


def _msg(*, raw_body: str, body: str = "x") -> ExtractedMessage:
    return ExtractedMessage(
        from_field="A <a@x.com>",
        date=datetime(2026, 5, 1, tzinfo=UTC),
        subject="s",
        body=body,
        raw_body=raw_body,
    )


def test_validator_flags_block_with_inner_unsplit_header():
    """A block whose raw_body still contains a `Fra:` header followed by body
    text is under-split — the regex layer missed a boundary inside it."""
    flagged = find_under_split_blocks(
        [
            _msg(
                raw_body=(
                    "Hei, mitt svar her.\n\n"
                    "Fra: Anne <anne@x.com>\n"
                    "Sendt: 1. mai 2023\n"
                    "Emne: Re: noe\n\n"
                    "Annes opprinnelige melding her.\n"
                )
            )
        ]
    )
    assert flagged == [0]


def test_validator_flags_asterisk_inner_header_too():
    """Same shape, but the inner header uses Outlook's `*Fra:*` form. With
    the regex fix in place the OUTER extractor would catch this, but if an
    even newer client variant slips through, the validator must still flag it."""
    flagged = find_under_split_blocks(
        [
            _msg(
                raw_body=(
                    "Hei,\n\n"
                    "*Fra:* X <x@x.com>\n"
                    "*Sendt:* 1. mai 2023 12:00\n"
                    "*Emne:* Re:\n\n"
                    "Indre meldings-innhold.\n"
                )
            )
        ]
    )
    assert flagged == [0]


def test_validator_ignores_block_with_no_inner_header():
    """A well-formed block with only its own body must NOT be flagged."""
    flagged = find_under_split_blocks(
        [_msg(raw_body="Hei, dette er et helt vanlig svar uten innfelt historikk.\n")]
    )
    assert flagged == []


def test_validator_ignores_trailing_header_with_no_body():
    """If the only `Fra:` line appears at the very end with no content after,
    it's likely the next boundary the outer extractor already split off."""
    flagged = find_under_split_blocks(
        [
            _msg(
                raw_body=(
                    "Hei,\n"
                    "Mitt svar.\n\n"
                    "Fra: X <x@x.com>\n"
                )
            )
        ]
    )
    assert flagged == []


def test_infer_missing_to_fills_from_prior_block():
    """In a chronological 2-party reply chain, every inline-reply block has
    `to_field=""` from the splitter. The inference pass fills each from the
    immediately-prior block's From."""
    from gb_automations.utils.email_splitting import infer_missing_to_fields

    blocks = [
        _msg(raw_body="a", body="a"),
        _msg(raw_body="b", body="b"),
        _msg(raw_body="c", body="c"),
    ]
    # Override from_field on each — _msg helper sets all to "A <a@x.com>".
    blocks[0].from_field = "Alice <alice@x.com>"
    blocks[1].from_field = "Bob <bob@x.com>"
    blocks[2].from_field = "Alice <alice@x.com>"

    infer_missing_to_fields(blocks)

    assert blocks[0].to_field == ""  # oldest — no prior
    assert blocks[1].to_field == "Alice <alice@x.com>"
    assert blocks[2].to_field == "Bob <bob@x.com>"


def test_infer_missing_to_preserves_explicit_to():
    """If a block already has a To from a real `Til:` header, don't overwrite."""
    from gb_automations.utils.email_splitting import infer_missing_to_fields

    blocks = [
        _msg(raw_body="a", body="a"),
        _msg(raw_body="b", body="b"),
    ]
    blocks[0].from_field = "Alice <alice@x.com>"
    blocks[1].from_field = "Bob <bob@x.com>"
    blocks[1].to_field = "Charlie <charlie@x.com>"  # explicit

    infer_missing_to_fields(blocks)

    assert blocks[1].to_field == "Charlie <charlie@x.com>"  # unchanged


def test_infer_missing_to_skips_when_prior_block_same_sender():
    """If the same person sent two messages back-to-back, the recipient is
    genuinely ambiguous from chronology alone. Leave empty."""
    from gb_automations.utils.email_splitting import infer_missing_to_fields

    blocks = [
        _msg(raw_body="a", body="a"),
        _msg(raw_body="b", body="b"),
    ]
    blocks[0].from_field = "Alice <alice@x.com>"
    blocks[1].from_field = "Alice <alice@x.com>"

    infer_missing_to_fields(blocks)

    assert blocks[1].to_field == ""


def test_infer_missing_to_handles_empty_list():
    from gb_automations.utils.email_splitting import infer_missing_to_fields

    infer_missing_to_fields([])  # no error


def test_validator_handles_missing_raw_body_safely():
    """Older test fixtures construct ExtractedMessage without raw_body — those
    must pass through without error and without being flagged."""
    flagged = find_under_split_blocks(
        [ExtractedMessage(from_field="A", date=PARENT_DATE, subject="s", body="b")]
    )
    assert flagged == []


# ============================================================
# External-sender banners (MailRisk + Microsoft Defender variants)
# ============================================================


def test_strip_mailrisk_banner_removes_whole_block():
    from gb_automations.utils.email_cleaning import strip_external_sender_banners

    body = (
        "*Caution:* This email originated from outside of the organization. Do *not*\n"
        "click links or open attachments unless you recognize the sender and know\n"
        "the content is safe. When in doubt, please report the email using the\n"
        "MailRisk button, and wait for IT support to assist you.\n\n"
        "Hmm, det var litt mye forskjell på prisene.\n"
    )
    out = strip_external_sender_banners(body)
    assert "MailRisk" not in out
    assert "originated from outside" not in out
    assert "Hmm, det var" in out


def test_strip_defender_banner_removes_single_line_variant():
    from gb_automations.utils.email_cleaning import strip_external_sender_banners

    body = (
        "You don't often get email from sender@x.com. Learn why this is important\n\n"
        "Actual content here.\n"
    )
    out = strip_external_sender_banners(body)
    assert "often get email" not in out
    assert "Learn why" not in out
    assert "Actual content" in out


def test_clean_body_strips_mailrisk_from_extracted_body():
    """The Notion-displayed body must be clean of banner pollution."""
    from gb_automations.utils.email_cleaning import clean_body

    body = (
        "*Caution:* This email originated from outside of the organization. Do *not*\n"
        "click links or open attachments unless you recognize the sender and know\n"
        "the content is safe. When in doubt, please report the email using the\n"
        "MailRisk button, and wait for IT support to assist you.\n\n"
        "Hmm, det var litt mye forskjell på prisene.\n\n"
        "Mvh\nPetter\n"
    )
    cleaned = clean_body(body)
    assert "MailRisk" not in cleaned
    assert "Caution" not in cleaned
    assert "Hmm, det var" in cleaned


def test_mailrisk_banner_is_not_a_boundary_but_clean_body_drops_it():
    """We chose to handle the banner as CONTENT-to-strip rather than as a
    boundary marker. Adjacent `*Fra:*` headers already supply the real
    boundary; treating the banner as another boundary created spurious empty
    extracted blocks. The banner gets removed from the final body by
    clean_body, which is what shows up in Notion."""
    from gb_automations.utils.email_cleaning import clean_body

    block_body = (
        "*Caution:* This email originated from outside of the organization. Do *not*\n"
        "click links or open attachments unless you recognize the sender and know\n"
        "the content is safe. When in doubt, please report the email using the\n"
        "MailRisk button, and wait for IT support to assist you.\n\n"
        "Petters opprinnelige melding.\n"
    )
    boundaries = find_reply_boundary_ranges(block_body)
    # No boundary on the banner alone (only `*Fra:*`/`From:`-style header
    # blocks or forward-divider lines open boundaries).
    assert boundaries == []
    cleaned = clean_body(block_body)
    assert "Caution" not in cleaned
    assert "MailRisk" not in cleaned
    assert "Petters opprinnelige melding" in cleaned


def test_clean_body_cuts_at_bilingual_signoff():
    """Norwegian senders writing internationally often use a `/`-joined
    bilingual sign-off: `Med vennlig hilsen / Best Regards`. The pre-fix
    `^Med vennlig hilsen,?\\s*$` anchor required nothing after the phrase, so
    the bilingual variant slipped through and the whole signature block
    leaked into the Notion-displayed body."""
    from gb_automations.utils.email_cleaning import clean_body

    body = (
        "Spennende! Ja, du må gjerne dele 😊\n\n"
        "Eksempel fra Wessel Park:\n\n"
        "Med vennlig hilsen / Best Regards\n\n"
        "*Ingeborg Kvamme Skar*\n"
        "Markedsansvarlig\n"
        "+47 977 99 616\n"
    )
    cleaned = clean_body(body)
    assert "Spennende" in cleaned
    assert "Med vennlig hilsen" not in cleaned
    assert "Best Regards" not in cleaned
    assert "Ingeborg" not in cleaned
    assert "977 99 616" not in cleaned


def test_clean_body_cuts_at_pipe_separated_signoff():
    """`|` is a less common but legitimate alternative to `/` for the bilingual
    join. Same pattern, same result."""
    from gb_automations.utils.email_cleaning import clean_body

    body = (
        "Body content.\n\n"
        "Mvh | Best regards\n\n"
        "Signature line\n"
    )
    cleaned = clean_body(body)
    assert "Body content" in cleaned
    assert "Mvh" not in cleaned
    assert "Signature line" not in cleaned


def test_clean_body_still_cuts_at_plain_signoff():
    """Regression: single-language sign-offs (the original case) must still
    work — the bilingual extension is additive."""
    from gb_automations.utils.email_cleaning import clean_body

    body = "Mitt svar.\n\nMvh\nTobias\n"
    cleaned = clean_body(body)
    assert "Mitt svar" in cleaned
    assert "Mvh" not in cleaned
    assert "Tobias" not in cleaned


def test_clean_body_strips_image_markers_by_default():
    """Legacy callers (no keep_image_markers) get all `[image: ...]` stripped."""
    from gb_automations.utils.email_cleaning import clean_body

    body = "Se vedlagt:\n[image: moodboard.jpg]\nViktig referanse."
    cleaned = clean_body(body)
    assert "[image:" not in cleaned
    assert "Se vedlagt" in cleaned
    assert "Viktig referanse" in cleaned


def test_clean_body_keeps_allow_listed_image_markers():
    """When the caller passes `keep_image_markers`, markers for those
    filenames survive and others don't."""
    from gb_automations.utils.email_cleaning import clean_body

    body = (
        "Se moodboard:\n[image: moodboard.jpg]\n"
        "Og logo:\n[image: signature_logo.png]\n"
        "Slutt."
    )
    cleaned = clean_body(body, keep_image_markers={"moodboard.jpg"})
    assert "[image: moodboard.jpg]" in cleaned
    assert "signature_logo.png" not in cleaned


def test_clean_body_keep_list_is_case_insensitive():
    """Filenames may differ in case between attachment metadata and body
    markers — match case-insensitively."""
    from gb_automations.utils.email_cleaning import clean_body

    body = "Reference:\n[image: IMAGE002.PNG]"
    cleaned = clean_body(body, keep_image_markers={"image002.png"})
    assert "[image: IMAGE002.PNG]" in cleaned


def test_clean_body_keep_list_empty_strips_all_markers():
    """Empty allow-list = strip all (same as default behavior)."""
    from gb_automations.utils.email_cleaning import clean_body

    body = "Body.\n[image: x.png]"
    cleaned = clean_body(body, keep_image_markers=set())
    assert "[image:" not in cleaned


def test_real_outlook_thread_ingeborg_signatures_cut_off():
    """Ingeborg's blocks use bilingual sign-offs (`Med vennlig hilsen / Best
    Regards`). The fix makes the cutter recognize the joined form so neither
    the phrase nor the contact info that follows it ends up in the body."""
    body = _load_fixture()
    blocks = extract_history_blocks(
        body, "Fwd: Re: Digital styling - tilbud Goldbox", PARENT_DATE
    )
    ingeborg_blocks = [b for b in blocks if "ingeborg" in b.from_field.lower()]
    assert ingeborg_blocks, "fixture should contain Ingeborg messages"
    for b in ingeborg_blocks:
        assert "Med vennlig hilsen" not in b.body, (
            f"sign-off leaked in {b.from_field[:30]}: {b.body[-200:]!r}"
        )
        # The phone number from Ingeborg's signature shouldn't leak either.
        assert "977 99 616" not in b.body, (
            f"phone-from-sig leaked: {b.body[-200:]!r}"
        )


def test_real_outlook_thread_no_mailrisk_in_extracted_bodies():
    """Regression: the previous live run produced rows whose `body` started
    with 'MailRisk button...' or '*Caution:* This email...'. After the strip
    fix, no extracted block should contain those phrases."""
    body = _load_fixture()
    blocks = extract_history_blocks(
        body, "Fwd: Re: Digital styling - tilbud Goldbox", PARENT_DATE
    )
    for b in blocks:
        assert "MailRisk" not in b.body, f"banner leaked into body: {b.body[:120]!r}"
        assert "originated from outside" not in b.body
        assert "Caution" not in b.body[:60]  # allow inside long bodies if quoted
