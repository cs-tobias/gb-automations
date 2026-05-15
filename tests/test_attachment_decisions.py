"""Tests for sync_thread._partition_attachments — the position-based filter
that decides which attachments are worth downloading + uploading to Drive.

Also covers `_attribute_attachments`, which assigns forwarded-thread
attachments to whichever extracted historical email's body actually mentions
the filename (so the file lands on the original sender's row, not the
forwarder's).

The repetition-based signature detection (_is_repeating_signature_image) is
not tested here because it requires a database session. End-to-end verification
covers that path against a real Postgres in the integration setup.
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
    assert _partition_attachments("Body text", []) == []


def test_partition_normal_attachment_uploads():
    body = "Hi,\nPlease see the attached PDF.\n\nMvh\nTobias"
    decisions = _partition_attachments(body, [_att("Tilbud.pdf")])
    assert len(decisions) == 1
    assert decisions[0].upload is True
    assert decisions[0].skip_reason == ""


def test_partition_signature_region_image_is_skipped():
    # Image referenced AFTER the signature marker → decoration.
    body = (
        "Hei,\nSe vedlagt PDF.\n\n"
        "Mvh\nTobias\n"
        "[image: logo.png]\n"  # below "Mvh" — signature region
    )
    decisions = _partition_attachments(body, [_att("logo.png", "image/png", 30_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is False
    assert decisions[0].skip_reason == "signature-region"


def test_partition_image_in_body_uploads():
    # Image referenced BEFORE the signature → meaningful body content.
    body = (
        "Hei,\n"
        "Se moodboard her:\n[image: moodboard.jpg]\n\n"
        "Mvh\nTobias"
    )
    decisions = _partition_attachments(body, [_att("moodboard.jpg", "image/jpeg", 2_000_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_image_without_signature_marker_uploads():
    # No signature marker at all → can't apply the position rule.
    # The repetition check (run later, with bytes) decides; here we accept.
    body = "Just a plain email body with no signature marker."
    decisions = _partition_attachments(body, [_att("img.png", "image/png", 20_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_image_without_body_reference_uploads():
    # Image attached but not referenced in body. Without a body reference,
    # the position rule can't fire — fall through to upload (and let the
    # repetition check sort signatures from real content later).
    body = "Hi,\nFile attached.\n\nMvh\nTobias"
    decisions = _partition_attachments(body, [_att("unreferenced.png", "image/png", 25_000)])
    assert len(decisions) == 1
    assert decisions[0].upload is True


def test_partition_preserves_order_and_handles_mixed():
    body = (
        "Hei,\n"
        "Se vedlagte filer og moodboard her:\n[image: moodboard.jpg]\n\n"
        "Mvh\nTobias\n"
        "[image: logo.png]\n"
    )
    atts = [
        _att("Tilbud.pdf"),                          # real attachment
        _att("moodboard.jpg", "image/jpeg", 1_000_000),  # body-region image
        _att("logo.png", "image/png", 25_000),        # signature-region image
    ]
    decisions = _partition_attachments(body, atts)
    assert [d.attachment.filename for d in decisions] == [
        "Tilbud.pdf",
        "moodboard.jpg",
        "logo.png",
    ]
    assert decisions[0].upload is True
    assert decisions[1].upload is True
    assert decisions[2].upload is False
    assert decisions[2].skip_reason == "signature-region"


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


def test_attribute_signature_region_stays_on_forwarder():
    # Forwarder's body has a logo BELOW their signature — that's their own
    # signature decoration, not anything the historical sender authored.
    # Even if an extracted email's body happens to contain "logo.png" as text,
    # the position-based skip on the forwarder's body fires first.
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
    assert by_synth == {}
    assert len(forwarder) == 1
    assert forwarder[0].upload is False
    assert forwarder[0].skip_reason == "signature-region"
