"""Tests for the per-thread attachment dedup tracker.

Two separate concerns the tracker keeps apart:

1. **Upload-dedup** (`links_by_sha1`): identical bytes anywhere in the thread
   are the same file regardless of sender, so we upload to Drive once and reuse
   the stored links. Pre-seeded from the durable ThreadAttachment table, so it
   survives across syncs.

2. **Row-attribution** (`attached_this_pass`): an attachment links to the row of
   the message that FIRST carried it *this sync pass*. Gmail/Outlook re-carries
   the identical bytes on every reply, so without this every reply row got the
   whole thread's attachments. This set starts empty each pass — so a re-sync
   still attaches the first message's files (it can't rely on links_by_sha1,
   which is pre-seeded and would make everything look "already seen").
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import gb_automations.sync.sync_thread as st
from gb_automations.clients.gmail import GmailAttachment, GmailMessage
from gb_automations.sync.sync_thread import (
    AttachmentDecision,
    ThreadAttachmentTracker,
)


def test_known_sha1_returns_stored_links():
    # Upload-dedup: once bytes are recorded with their Drive links, a later
    # re-carry of the SAME bytes reuses them instead of re-uploading.
    tracker = ThreadAttachmentTracker()
    links = [{"name": "Plan.dwg", "url": "https://drive/abc"}]
    tracker.links_by_sha1["photo-sha"] = links

    assert "photo-sha" in tracker.sha1s
    assert tracker.links_by_sha1.get("photo-sha") == links


def test_recarry_across_senders_same_bytes_is_known():
    # A reply quotes the earlier message and Gmail re-carries the IDENTICAL bytes
    # under the replier's name. Same sha1 → known for upload-dedup (no re-upload).
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1["img-sha"] = [
        {"name": "IMG_2555.jpeg", "url": "https://drive/img"}
    ]
    assert "img-sha" in tracker.sha1s


def test_attached_this_pass_starts_empty_and_is_distinct_from_links():
    # The fix's core invariant: attached_this_pass is independent of the
    # pre-seeded links_by_sha1. A re-sync seeds links_by_sha1 (bytes already on
    # Drive) but attached_this_pass is still empty — so the first message that
    # carries the file this pass will attach it, and only later re-carries skip.
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1["seeded-sha"] = [{"name": "f.png", "url": "https://drive/f"}]
    assert tracker.attached_this_pass == set()
    assert "seeded-sha" not in tracker.attached_this_pass

    # Simulate the first message of the pass attaching it.
    tracker.attached_this_pass.add("seeded-sha")
    # A later re-carry in the same pass would now be recognized and skipped.
    assert "seeded-sha" in tracker.attached_this_pass


def test_distinct_bytes_are_independent():
    # Two genuinely different files (distinct bytes → distinct sha1) — both are
    # uploaded; neither suppresses the other, even with the same filename.
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1["alice-sha"] = [
        {"name": "screenshot.png", "url": "https://drive/a"}
    ]
    assert "bob-sha" not in tracker.sha1s


def test_revised_file_same_name_is_not_suppressed():
    # Regression for the removed fuzzy ±10KB rule: a revised file re-sent under
    # the SAME name with different bytes (different sha1) must NOT be treated as
    # a duplicate. The team wants both versions in Notion.
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1["v1-sha"] = [
        {"name": "revisjon-3.pdf", "url": "https://drive/v1"}
    ]
    assert "v2-sha" not in tracker.sha1s


def test_sha1s_view_reflects_recorded_links():
    tracker = ThreadAttachmentTracker()
    assert tracker.sha1s == set()
    tracker.links_by_sha1["a"] = [{"name": "f", "url": "u"}]
    tracker.links_by_sha1["b"] = []
    assert tracker.sha1s == {"a", "b"}


# --------------------------------------------------------------------------
# Download short-circuit: a re-carried (filename, size) is not re-fetched.
# This is the perf fix — Outlook re-embeds identical inline bytes on every
# reply, and we must not pay a Gmail round-trip per re-carry just to hash and
# discard. Detection is unchanged: the byte-sha1 is still authoritative; the
# (filename,size) map only ever returns a sha1 we computed from real bytes
# earlier THIS pass.
# --------------------------------------------------------------------------


def _att(filename: str, size: int) -> GmailAttachment:
    return GmailAttachment(
        filename=filename,
        mime_type="image/png",
        size=size,
        attachment_id=f"att-{filename}-{size}",
    )


def _msg(attachments: list[GmailAttachment]) -> GmailMessage:
    return GmailMessage(
        message_id="m1",
        thread_id="t1",
        date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        subject="Svar: Tekstil liftgardin",
        from_field="a@b.no",
        to_field="c@d.no",
        cc_field="",
        plain_body="",
        attachments=attachments,
        label_ids=[],
    )


class _NullSession:
    async def get(self, *a, **k):
        return None

    async def execute(self, *a, **k):
        return None


def _run_upload(monkeypatch, decisions, tracker):
    """Drive `_upload_attachments` with all external I/O faked out.

    Returns the list of message_ids passed to get_attachment_bytes so the test
    can assert how many real downloads happened.
    """
    fetched: list[str] = []

    def fake_get_bytes(user_email, message_id, attachment_id):
        fetched.append(attachment_id)
        # The attachment_id encodes name+size (see _att), so identical name+size
        # yields identical bytes (the re-carry case) and distinct ones differ —
        # matching how real Gmail bytes track the (filename, size) fingerprint.
        return attachment_id.encode()

    uploaded_calls: list = []

    def fake_upload(user_email, folder_path, filename, mime, content):
        uploaded_calls.append(filename)
        return f"https://drive/{filename}"

    monkeypatch.setattr(st.gmail_client, "get_attachment_bytes", fake_get_bytes)
    monkeypatch.setattr(st.drive_client, "upload_attachment", fake_upload)
    # NAS off => received_dirs empty => _ensure_in_nas is a no-op, no FS calls.
    monkeypatch.setattr(st.settings, "sync_nas_folders", False)
    monkeypatch.setattr(st.settings, "attachments_folder_name", "Vedlegg")

    result = asyncio.run(
        st._upload_attachments(
            parent_msg=_msg([d.attachment for d in decisions]),
            decisions=decisions,
            attributed_sender="a@b.no",
            user_email="me@goldbox.no",
            session=_NullSession(),
            thread_tracker=tracker,
            project_label_paths=["Prosjekt/2026/Acme"],
            project_page_ids=[],
            email_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
            tags=[],
        )
    )
    return fetched, uploaded_calls, result


def test_recarried_namesize_is_not_redownloaded(monkeypatch):
    # Three decisions for the SAME re-carried inline image (identical name+size),
    # as Outlook produces across a 3-reply thread. Only the FIRST should hit
    # Gmail; the other two reuse the sha1 from sha1_by_namesize and skip the
    # download entirely.
    tracker = ThreadAttachmentTracker()
    decisions = [
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
    ]
    fetched, uploaded_calls, _ = _run_upload(monkeypatch, decisions, tracker)

    assert len(fetched) == 1, "re-carried copies must not re-download from Gmail"
    # First copy uploads once (one project folder); re-carries skip via
    # attached_this_pass — no second upload, no duplicate row link.
    assert len(uploaded_calls) == 1


def test_distinct_size_still_downloads(monkeypatch):
    # Same filename but DIFFERENT byte size = a different file (revised version).
    # Detection must NOT be weakened: both download and both upload.
    tracker = ThreadAttachmentTracker()
    decisions = [
        AttachmentDecision(_att("revisjon.pdf", 1000), upload=True),
        AttachmentDecision(_att("revisjon.pdf", 2000), upload=True),
    ]
    fetched, uploaded_calls, _ = _run_upload(monkeypatch, decisions, tracker)

    assert len(fetched) == 2
    assert len(uploaded_calls) == 2
