"""Tests for sync_thread._partition_attachments — the byte-free filter that
decides which attachments are worth downloading + uploading to Drive.

Two structural skip rules, both safe against false positives on real files:
  - tiny-image (< 1 KB image/*) — Office-generated signature thumbnails and
    tracking pixels, never real content.
  - inline-signature-image (image/* + Content-ID + `<img src=cid:>` referenced
    in HTML body + disposition != "attachment") — the structural shape every
    MUA-rendered signature decoration produces. A user-attached file is never
    `cid:`-referenced (it wasn't dropped into the HTML editor), so this skip
    is safe on first sighting. The disposition guard preserves the rare case
    where a sender BOTH attached and inline-referenced the same image.

All other earlier heuristics (statistical sender-repeat, fuzzy ±size same-name,
position-based signature-region) were removed because they could drop real
distinct files. Byte-identical re-carries are still de-duplicated for upload
by content sha1 in the upload loop, but that re-links the row instead of
dropping the file.

Also covers `_attribute_attachments`, which assigns forwarded-thread
attachments to whichever extracted historical email's body actually mentions
the filename (so the file lands on the original sender's row, not the
forwarder's).
"""

from datetime import UTC, datetime

from gb_automations.clients.gmail import GmailAttachment, GmailMessage
from gb_automations.sync.sync_thread import (
    _attribute_attachments,
    _partition_attachments,
)
from gb_automations.utils.email_splitting import (
    ExtractedMessage,
    synthetic_message_id,
)


def _att(name: str, mime: str = "application/pdf", size: int = 100_000) -> GmailAttachment:
    return GmailAttachment(filename=name, mime_type=mime, size=size, attachment_id="abc")


def test_partition_no_attachments_returns_empty():
    assert _partition_attachments([]) == []


def test_partition_normal_attachment_uploads():
    decisions = _partition_attachments([_att("Tilbud.pdf")])
    assert len(decisions) == 1
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_image_near_signature_still_uploads():
    # Regression: previously an image whose `[image:]` marker sat below the
    # signature line was dropped as "signature-region". Real photos in short
    # emails land there too, so we now upload it. Only the sub-1KB tiny-image
    # rule can skip now.
    decisions = _partition_attachments([_att("logo.png", "image/png", 30_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_image_in_body_uploads():
    decisions = _partition_attachments([_att("moodboard.jpg", "image/jpeg", 2_000_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_tiny_image_under_1kb_is_skipped():
    # Word's `~WRDxxxx.jpg` and other Office-generated signature thumbnails
    # consistently sit below 1 KB. Skip without needing to download bytes.
    decisions = _partition_attachments([_att("~WRD0002.jpg", "image/jpeg", 800)])
    assert len(decisions) == 1
    assert decisions[0].upload is False
    assert decisions[0].skip_reason == "tiny-image"


def test_partition_small_but_real_image_still_uploads():
    # 4.6 KB is small enough to be a logo but well above the 1 KB threshold —
    # clients can legitimately send small brand assets as real attachments.
    decisions = _partition_attachments([_att("brand-logo.png", "image/png", 4_600)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_inline_signature_image_skipped():
    """An image with a Content-ID, referenced by `<img src=cid:>` in the HTML
    body (inline_ref_count >= 1), and disposition=inline is structurally a
    signature decoration. The sender's MUA inserted it as inline content —
    a user-attached file is never `cid:`-referenced."""
    att = GmailAttachment(
        filename="image001.png",
        mime_type="image/png",
        size=4_761,
        attachment_id="abc",
        content_id="ii_x@host",
        inline_ref_count=2,
        content_disposition="inline",
    )
    decisions = _partition_attachments([att])
    assert len(decisions) == 1
    assert decisions[0].upload is False
    assert decisions[0].skip_reason == "inline-signature-image"


def test_partition_inline_signature_no_disposition_still_skipped():
    """Outlook inline parts frequently omit Content-Disposition entirely.
    A missing disposition is treated as "not attachment" so the structural
    skip still fires."""
    att = GmailAttachment(
        filename="image001.png",
        mime_type="image/png",
        size=4_761,
        attachment_id="abc",
        content_id="ii_x@host",
        inline_ref_count=1,
        content_disposition=None,
    )
    decisions = _partition_attachments([att])
    assert decisions[0].upload is False
    assert decisions[0].skip_reason == "inline-signature-image"


def test_partition_attached_and_inline_referenced_uploads():
    """Rare dual-mark case: sender BOTH attached a file AND embedded it inline
    (preview-in-body) with a single MIME part. An explicit
    `Content-Disposition: attachment` overrides the inline-signature skip so
    the real attachment isn't dropped."""
    att = GmailAttachment(
        filename="floorplan.png",
        mime_type="image/png",
        size=120_000,
        attachment_id="abc",
        content_id="ii_x@host",
        inline_ref_count=1,
        content_disposition="attachment",
    )
    decisions = _partition_attachments([att])
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_inline_image_not_html_referenced_uploads():
    """An attachment that has a Content-ID but is NOT actually referenced by
    `<img src=cid:>` in the HTML (inline_ref_count == 0) is a real attached
    image the MUA happened to label. Don't skip — only HTML-referenced parts
    are structurally signatures."""
    att = GmailAttachment(
        filename="photo.png",
        mime_type="image/png",
        size=80_000,
        attachment_id="abc",
        content_id="ii_x@host",
        inline_ref_count=0,
        content_disposition="inline",
    )
    decisions = _partition_attachments([att])
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_inline_pdf_with_cid_uploads():
    """The skip is gated on image/* — a PDF (or any non-image) that happens
    to have a Content-ID + cid reference is unusual but always uploads. We
    will not drop a real document on the rare structural quirk."""
    att = GmailAttachment(
        filename="Tilbud.pdf",
        mime_type="application/pdf",
        size=200_000,
        attachment_id="abc",
        content_id="ii_x@host",
        inline_ref_count=1,
        content_disposition="inline",
    )
    decisions = _partition_attachments([att])
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_tiny_non_image_is_not_skipped():
    # Only image/* gets the tiny-size skip. A tiny PDF or vCard is rare but
    # legitimate; don't skip those.
    decisions = _partition_attachments([_att("contact.vcf", "text/vcard", 500)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_preserves_order_and_handles_mixed():
    atts = [
        _att("Tilbud.pdf"),                              # real attachment
        _att("moodboard.jpg", "image/jpeg", 1_000_000),  # real photo
        _att("~WRD0001.jpg", "image/jpeg", 600),         # tiny signature thumb
    ]
    decisions = _partition_attachments(atts)
    assert [d.attachment.filename for d in decisions] == [
        "Tilbud.pdf",
        "moodboard.jpg",
        "~WRD0001.jpg",
    ]
    assert decisions[0].upload is True
    assert decisions[1].upload is True
    assert decisions[2].upload is False
    assert decisions[2].skip_reason == "tiny-image"


# ============================================================
# _attribute_attachments — forwarded-thread attribution
# ============================================================


def _msg(
    *,
    message_id: str = "PARENT",
    from_field: str = "Forwarder <fwd@example.com>",
    body: str = "",
    attachments: list[GmailAttachment] | None = None,
) -> GmailMessage:
    return GmailMessage(
        message_id=message_id,
        thread_id="THREAD",
        date=datetime(2026, 5, 1, tzinfo=UTC),
        subject="Fwd: Tilbud",
        from_field=from_field,
        to_field="me@example.com",
        cc_field="",
        plain_body=body,
        attachments=attachments or [],
        label_ids=[],
    )


def _extracted(from_field: str, body: str, day: int) -> ExtractedMessage:
    return ExtractedMessage(
        from_field=from_field,
        date=datetime(2026, 4, day, tzinfo=UTC),
        subject="Tilbud",
        body=body,
    )


def test_attribute_no_extracted_keeps_everything_on_forwarder():
    parent = _msg(
        body="Videresender, mvh\nTobias",
        attachments=[_att("Tilbud.pdf")],
    )
    forwarder, by_synth = _attribute_attachments(parent, [])
    assert by_synth == {}
    assert len(forwarder) == 1
    assert forwarder[0].attachment.filename == "Tilbud.pdf"


def test_attribute_file_to_extracted_email_that_mentions_it():
    # Original sender (Anne) mentioned Tilbud.pdf in her body. Forwarder (Bob)
    # forwarded the whole thread without commentary. File should be attributed
    # to Anne's extracted row, not Bob's.
    parent = _msg(
        from_field="Bob <bob@example.com>",
        body="Videresender til deg.",
        attachments=[_att("Tilbud.pdf")],
    )
    anne = _extracted(
        from_field="Anne Hansen <anne@example.com>",
        body="Hei,\nSe vedlagte Tilbud.pdf\n\nMvh\nAnne",
        day=10,
    )
    forwarder, by_synth = _attribute_attachments(parent, [anne])
    anne_synth = synthetic_message_id(parent.message_id, anne.from_field, anne.body)
    assert forwarder == []
    assert list(by_synth.keys()) == [anne_synth]
    assert [d.attachment.filename for d in by_synth[anne_synth]] == ["Tilbud.pdf"]


def test_attribute_unmentioned_file_falls_back_to_forwarder():
    parent = _msg(
        body="Videresender.",
        attachments=[_att("mystery.pdf")],
    )
    anne = _extracted(
        from_field="Anne <anne@example.com>",
        body="Hei, ingen vedlegg her.",
        day=10,
    )
    forwarder, by_synth = _attribute_attachments(parent, [anne])
    assert by_synth == {}
    assert [d.attachment.filename for d in forwarder] == ["mystery.pdf"]


def test_attribute_oldest_mention_wins():
    # Both Anne (April 10) and Bob (April 20) reference floorplan.pdf — the
    # oldest one is treated as the original sender.
    parent = _msg(attachments=[_att("floorplan.pdf")])
    anne = _extracted(
        from_field="Anne <anne@example.com>",
        body="Vedlagt: floorplan.pdf",
        day=10,
    )
    bob = _extracted(
        from_field="Bob <bob@example.com>",
        body="Takk, så på floorplan.pdf",
        day=20,
    )
    _, by_synth = _attribute_attachments(parent, [anne, bob])
    anne_synth = synthetic_message_id(parent.message_id, anne.from_field, anne.body)
    assert list(by_synth.keys()) == [anne_synth]


def test_attribute_empty_forwarder_body_falls_back_to_oldest_non_forwarder():
    # Outlook-style forward: outer body is empty (no commentary), no filename
    # markers survived in any extracted block. The fallback should put the
    # attachment on the oldest non-forwarder extracted message instead of
    # leaving it on the empty forwarder row.
    parent = _msg(
        from_field="Petter <petter@example.com>",
        body="",
        attachments=[_att("image002.png", "image/png", 443_000)],
    )
    ingeborg_old = _extracted(
        from_field="Ingeborg <ingeborg@example.com>",
        body="Tilbud-tråd, ingen filnavn nevnt her.",
        day=10,
    )
    ingeborg_new = _extracted(
        from_field="Ingeborg <ingeborg@example.com>",
        body="Senere svar.",
        day=20,
    )
    forwarder, by_synth = _attribute_attachments(parent, [ingeborg_old, ingeborg_new])
    oldest_synth = synthetic_message_id(
        parent.message_id, ingeborg_old.from_field, ingeborg_old.body
    )
    assert forwarder == []
    assert list(by_synth.keys()) == [oldest_synth]
    assert [d.attachment.filename for d in by_synth[oldest_synth]] == ["image002.png"]


def test_attribute_empty_forwarder_body_with_only_self_blocks_stays_on_forwarder():
    # Forwarder's own prior reply got extracted (e.g. they wrote, then
    # forwarded the chain). With no non-forwarder block to claim the file,
    # the fallback doesn't fire — file stays on the forwarder row.
    parent = _msg(
        from_field="Petter <petter@example.com>",
        body="",
        attachments=[_att("mystery.pdf")],
    )
    self_block = _extracted(
        from_field="Petter <petter@example.com>",
        body="Mitt tidligere svar.",
        day=10,
    )
    forwarder, by_synth = _attribute_attachments(parent, [self_block])
    assert by_synth == {}
    assert [d.attachment.filename for d in forwarder] == ["mystery.pdf"]


def test_attribute_real_marker_match_wins_over_fallback():
    # Even when forwarder body is empty, a real `[image: NAME]` match on a
    # non-oldest block beats the fallback (which picks the oldest non-forwarder).
    parent = _msg(
        from_field="Petter <petter@example.com>",
        body="",
        attachments=[_att("image002.png", "image/png", 443_000)],
    )
    older = _extracted(
        from_field="Ingeborg <ingeborg@example.com>",
        body="Eldre svar uten filnavn.",
        day=10,
    )
    newer = ExtractedMessage(
        from_field="Ingeborg <ingeborg@example.com>",
        date=datetime(2026, 4, 20, tzinfo=UTC),
        subject="Tilbud",
        body="Cleaned body uten filnavn.",
        raw_body="Her er [image: image002.png] som referert.",
    )
    _, by_synth = _attribute_attachments(parent, [older, newer])
    newer_synth = synthetic_message_id(parent.message_id, newer.from_field, newer.body)
    assert list(by_synth.keys()) == [newer_synth]


def test_attribute_uploadable_image_goes_to_mentioning_block():
    # With the position-based signature-region skip removed, a >1 KB image
    # referenced exactly once is uploadable. When an extracted email's body
    # mentions the filename, ownership goes to that block — not the forwarder.
    parent = _msg(
        body="Videresender.\n\nMvh\nBob\n[image: logo.png]\n",
        attachments=[_att("logo.png", "image/png", 25_000)],
    )
    anne = _extracted(
        from_field="Anne <anne@example.com>",
        body="logo.png var fin",
        day=10,
    )
    forwarder, by_synth = _attribute_attachments(parent, [anne])
    anne_synth = synthetic_message_id(parent.message_id, anne.from_field, anne.body)
    assert forwarder == []
    assert list(by_synth.keys()) == [anne_synth]
    assert by_synth[anne_synth][0].upload is True
