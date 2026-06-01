"""Tests for the (sha1, drive_folder_path) attachment dedup layer.

Two separate concerns the tracker keeps apart:

1. **Upload-dedup** (`links_by_sha1_folder`): identical bytes landing in the
   same project's Drive folder are the same file regardless of which thread
   or sender carries them. The tracker holds one entry per (sha1, folder), so:
     - Same bytes → same folder → reuse the existing Drive link (no upload).
     - Same bytes → different folder → upload fresh (each project's Drive
       folder stays self-contained).
   Pre-seeded from the durable `AttachmentBlob` table at sync start, so it
   survives across syncs AND across threads.

2. **Row-attribution** (`attached_this_pass`): an attachment links to the row
   of the message that FIRST carried it *this sync pass*. Gmail/Outlook
   re-carries the identical bytes on every reply, so without this every reply
   row got the whole thread's attachments. This set starts empty each pass —
   so a re-sync still attaches the first message's files (it can't rely on
   `links_by_sha1_folder`, which is pre-seeded and would make everything look
   "already seen").
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


# --------------------------------------------------------------------------
# In-memory tracker invariants
# --------------------------------------------------------------------------


def test_links_for_known_folder_is_reused_unknown_is_missing():
    """The per-folder split that drives the upload loop: cached folders are
    returned as `known`, uncached folders as `missing`."""
    tracker = ThreadAttachmentTracker()
    folder_a = ("Vedlegg", "Prosjekt", "2026", "Acme")
    folder_b = ("Vedlegg", "Prosjekt", "2026", "Beta")
    tracker.links_by_sha1_folder[("sha-A", folder_a)] = {
        "name": "plan.pdf",
        "url": "https://drive/a",
    }

    known, missing = tracker.links_for("sha-A", [folder_a, folder_b])
    assert known == [{"name": "plan.pdf", "url": "https://drive/a"}]
    assert missing == [folder_b]


def test_attached_this_pass_starts_empty_and_is_distinct_from_links():
    """attached_this_pass is independent of the pre-seeded links table. A
    re-sync seeds links (bytes already on Drive) but attached_this_pass is
    still empty — so the first message that carries the file this pass attaches
    it, and only later re-carries skip."""
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1_folder[("seeded-sha", ("Vedlegg", "Acme"))] = {
        "name": "f.png",
        "url": "https://drive/f",
    }
    assert tracker.attached_this_pass == set()
    tracker.attached_this_pass.add("seeded-sha")
    assert "seeded-sha" in tracker.attached_this_pass


def test_links_for_with_no_folders_returns_empty():
    tracker = ThreadAttachmentTracker()
    known, missing = tracker.links_for("any-sha", [])
    assert known == []
    assert missing == []


# --------------------------------------------------------------------------
# `_upload_attachments` end-to-end: per-folder dedup + cross-thread behavior.
# All external I/O faked out — we assert on what would have been uploaded /
# downloaded / persisted.
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


class _RecordingSession:
    """Collects every `session.execute(...)` call so persistence is observable.

    `session.get(...)` returns None (NAS folder lookup misses → NAS write
    skipped). `session.execute(...)` records the statement object and returns
    None — the upload loop does not read the result.
    """

    def __init__(self) -> None:
        self.executed: list = []

    async def get(self, *a, **k):
        return None

    async def execute(self, statement, *a, **k):
        self.executed.append(statement)
        return None


def _run_upload(
    monkeypatch,
    decisions,
    tracker,
    project_label_paths=("Prosjekt/2026/Acme",),
):
    """Drive `_upload_attachments` with all external I/O faked out.

    Returns (fetched_attachment_ids, uploaded_(folder, filename) tuples,
    persisted_inserts, returned_links).
    """
    fetched: list[str] = []

    def fake_get_bytes(user_email, message_id, attachment_id):
        fetched.append(attachment_id)
        # The attachment_id encodes name+size (see _att), so identical name+size
        # yields identical bytes (the re-carry / cross-thread same-file case)
        # and distinct ones differ.
        return attachment_id.encode()

    uploaded_calls: list[tuple[tuple[str, ...], str]] = []

    def fake_upload(user_email, folder_path, filename, mime, content):
        uploaded_calls.append((folder_path, filename))
        return f"https://drive/{'/'.join(folder_path)}/{filename}"

    monkeypatch.setattr(st.gmail_client, "get_attachment_bytes", fake_get_bytes)
    monkeypatch.setattr(st.drive_client, "upload_attachment", fake_upload)
    monkeypatch.setattr(st.settings, "sync_nas_folders", False)
    monkeypatch.setattr(st.settings, "attachments_folder_name", "Vedlegg")

    session = _RecordingSession()
    result = asyncio.run(
        st._upload_attachments(
            parent_msg=_msg([d.attachment for d in decisions]),
            decisions=decisions,
            attributed_sender="a@b.no",
            user_email="me@goldbox.no",
            session=session,
            thread_tracker=tracker,
            project_label_paths=list(project_label_paths),
            project_page_ids=[],
            email_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
            tags=[],
        )
    )
    return fetched, uploaded_calls, session.executed, result


def test_same_bytes_same_folder_different_thread_reuses_link(monkeypatch):
    """The cross-thread dedup core case: a pre-seeded blob for the bytes
    landing in this thread's project folder means NO Drive upload happens
    and the existing link is reused on the row."""
    tracker = ThreadAttachmentTracker()
    folder = ("Vedlegg", "Prosjekt", "2026", "Acme")
    tracker.links_by_sha1_folder[
        # The sha1 for the bytes our fake produces for this attachment_id.
        (
            __import__("hashlib").sha1(b"att-shared.pdf-2048").hexdigest(),
            folder,
        )
    ] = {"name": "shared.pdf", "url": "https://drive/existing"}

    decisions = [AttachmentDecision(_att("shared.pdf", 2048), upload=True)]
    fetched, uploaded, persisted, result = _run_upload(monkeypatch, decisions, tracker)

    # Bytes still get downloaded (we hash to confirm sha1 match). But no
    # Drive upload happens — the cached link is reused.
    assert uploaded == []
    # The row gets linked to the cached Drive URL.
    assert result == [{"name": "shared.pdf", "url": "https://drive/existing"}]
    # No persistence call — nothing new to record.
    assert persisted == []


def test_same_bytes_different_folder_uploads_fresh(monkeypatch):
    """Cached blob for project A doesn't cover project B. The second project's
    folder gets a fresh upload and a new blob row is persisted."""
    tracker = ThreadAttachmentTracker()
    sha1 = __import__("hashlib").sha1(b"att-shared.pdf-2048").hexdigest()
    folder_a = ("Vedlegg", "Prosjekt", "2026", "Acme")
    tracker.links_by_sha1_folder[(sha1, folder_a)] = {
        "name": "shared.pdf",
        "url": "https://drive/existing-a",
    }

    decisions = [AttachmentDecision(_att("shared.pdf", 2048), upload=True)]
    fetched, uploaded, persisted, result = _run_upload(
        monkeypatch,
        decisions,
        tracker,
        project_label_paths=("Prosjekt/2026/Acme", "Prosjekt/2026/Beta"),
    )

    # Only the Beta folder uploads.
    assert len(uploaded) == 1
    assert uploaded[0][0] == ("Vedlegg", "Prosjekt", "2026", "Beta")
    # Row gets both links: cached Acme + fresh Beta.
    urls = {link["url"] for link in result}
    assert "https://drive/existing-a" in urls
    assert any(u.startswith("https://drive/Vedlegg/Prosjekt/2026/Beta/") for u in urls)
    # One blob insert for the new folder.
    assert len(persisted) == 1


def test_recarried_namesize_is_not_redownloaded(monkeypatch):
    """Three decisions for the SAME re-carried inline image (identical
    name+size), as Outlook produces across a 3-reply thread. Only the FIRST
    should hit Gmail; the others reuse the sha1 from sha1_by_namesize and skip
    the download. The first attaches; the others skip via attached_this_pass."""
    tracker = ThreadAttachmentTracker()
    decisions = [
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
        AttachmentDecision(_att("Outlook-Bilde.png", 4096), upload=True),
    ]
    fetched, uploaded, _, _ = _run_upload(monkeypatch, decisions, tracker)

    assert len(fetched) == 1, "re-carried copies must not re-download from Gmail"
    assert len(uploaded) == 1


def test_distinct_size_still_downloads_and_uploads(monkeypatch):
    """Same filename but different size = a different file (revised version).
    Both download and both upload."""
    tracker = ThreadAttachmentTracker()
    decisions = [
        AttachmentDecision(_att("revisjon.pdf", 1000), upload=True),
        AttachmentDecision(_att("revisjon.pdf", 2000), upload=True),
    ]
    fetched, uploaded, _, _ = _run_upload(monkeypatch, decisions, tracker)

    assert len(fetched) == 2
    assert len(uploaded) == 2


def test_fresh_upload_persists_blob_per_folder(monkeypatch):
    """One attachment, two project folders, no cached blobs. Two uploads, two
    persist calls, and the tracker holds both (sha1, folder) entries
    afterwards so a third decision for the same bytes would reuse."""
    tracker = ThreadAttachmentTracker()
    decisions = [AttachmentDecision(_att("plan.pdf", 5000), upload=True)]
    fetched, uploaded, persisted, result = _run_upload(
        monkeypatch,
        decisions,
        tracker,
        project_label_paths=("Prosjekt/2026/Acme", "Prosjekt/2026/Beta"),
    )

    assert len(uploaded) == 2
    assert len(persisted) == 2
    assert len(result) == 2
    sha1 = __import__("hashlib").sha1(b"att-plan.pdf-5000").hexdigest()
    assert (sha1, ("Vedlegg", "Prosjekt", "2026", "Acme")) in tracker.links_by_sha1_folder
    assert (sha1, ("Vedlegg", "Prosjekt", "2026", "Beta")) in tracker.links_by_sha1_folder
