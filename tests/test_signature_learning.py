"""Tests for `_check_or_record_signature_image` — the per-contact, byte-exact,
threshold-gated signature-image learner.

What this layer adds on top of the structural inline-signature rule:
  - structural rule (in _partition_attachments) catches signatures whose MUA
    marked them with Content-ID + `<img src="cid:...">`. Most MUAs do.
  - this layer catches the long tail: a logo attached as plain
    `Content-Disposition: attachment` with no cid reference.

Bar for "is a signature": same byte sha1 from the same sender across N
DISTINCT Gmail threads (not N messages). 8 replies in one thread carrying
the same logo = 1, not 8. Once at threshold the bytes are skipped forever
for that sender. Recovery: status='allowlisted'.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from gb_automations.models import ContactSignatureImage
from gb_automations.sync.sync_thread import _check_or_record_signature_image


class _FakeSession:
    """In-memory stand-in for AsyncSession scoped to ContactSignatureImage.

    Models `session.get((sender, sha1))` and `session.execute(statement)` for
    INSERT and UPDATE on this one table. Rows live in `self.rows`, keyed by
    `(sender_email, content_sha1)`.

    INSERT … ON CONFLICT DO NOTHING is honored (the second insert is a no-op).
    UPDATE matches on the WHERE clause's (sender_email, content_sha1) eq pair
    that the helper writes.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], ContactSignatureImage] = {}

    async def get(self, model, pk):
        # Only ContactSignatureImage is tracked; other model lookups (e.g.
        # ProjectFolder during NAS reconciliation) return None so the upload
        # loop falls through to its empty-cache path — fine for these tests
        # where NAS is disabled via the monkeypatch in _run_upload.
        if model is ContactSignatureImage:
            return self.rows.get(pk)
        return None

    async def execute(self, statement):
        # SQLAlchemy core statement shape: we read the compiled type + values
        # off the statement. Cheaper than a real DB and predictable.
        compiled = statement.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled).lower()
        if sql.startswith("insert into contact_signature_images"):
            params = _extract_insert_values(statement)
            key = (params["sender_email"], params["content_sha1"])
            if key not in self.rows:
                self.rows[key] = ContactSignatureImage(
                    sender_email=params["sender_email"],
                    content_sha1=params["content_sha1"],
                    thread_seen_count=params["thread_seen_count"],
                    last_thread_id=params["last_thread_id"],
                    first_filename=params["first_filename"],
                    status=params["status"],
                    first_seen_at=datetime.now(timezone.utc),
                    updated_at=datetime.now(timezone.utc),
                )
            # else: ON CONFLICT DO NOTHING — leave row alone.
            return None
        if sql.startswith("update contact_signature_images"):
            sender, sha1 = _extract_update_where(statement)
            values = _extract_update_values(statement)
            row = self.rows.get((sender, sha1))
            if row is not None:
                for k, v in values.items():
                    setattr(row, k, v)
                row.updated_at = datetime.now(timezone.utc)
            return None
        # Tolerate other tables (e.g. AttachmentBlob INSERTs) — those are
        # exercised end-to-end by test_attachment_blob_dedup.py and need to
        # be no-ops here so this session can be plugged into _upload_attachments
        # for integration-style tests of the learn-vs-re-carry ordering.
        return None


def _compiled_params(statement) -> dict:
    """Bound parameters from a compiled statement.

    Works uniformly for INSERT and UPDATE — the helper's INSERT uses
    `.values(literal=...)` (no params) so values come from
    `statement.parameters`; the UPDATE binds parameters through `.values(...)`
    which surfaces as `compile().params`. Try both.
    """
    compiled = statement.compile()
    params = dict(compiled.params)
    if not params:
        # Fallback for constructs whose values are stored on the statement
        # directly rather than as compile params.
        raw = getattr(statement, "parameters", None) or getattr(statement, "_values", None)
        if raw:
            params = {k: v for k, v in dict(raw).items()}
    return params


def _extract_insert_values(statement) -> dict:
    return _compiled_params(statement)


def _extract_update_values(statement) -> dict:
    return _compiled_params(statement)


def _extract_update_where(statement) -> tuple[str, str]:
    """Pull the (sender_email, content_sha1) literals from the UPDATE WHERE."""
    import re

    compiled = statement.compile(compile_kwargs={"literal_binds": True})
    sql = str(compiled)
    # The helper writes WHERE exactly: `sender_email = 'x' AND content_sha1 = 'y'`.
    m_sender = re.search(r"sender_email = '([^']*)'", sql)
    m_sha = re.search(r"content_sha1 = '([^']*)'", sql)
    assert m_sender and m_sha, f"could not parse WHERE: {sql!r}"
    return m_sender.group(1), m_sha.group(1)


def _call(
    session: _FakeSession,
    *,
    sender: str = "anne@example.com",
    sha1: str = "a" * 40,
    thread_id: str = "thread-1",
    mime: str = "image/png",
    filename: str = "logo.png",
) -> bool:
    return asyncio.run(
        _check_or_record_signature_image(
            session=session,
            sender_email=sender,
            content_sha1=sha1,
            thread_id=thread_id,
            mime_type=mime,
            filename=filename,
        )
    )


# --------------------------------------------------------------------------
# Threshold + learning lifecycle
# --------------------------------------------------------------------------


def test_first_sighting_records_at_count_1_does_not_skip():
    session = _FakeSession()
    assert _call(session, thread_id="t1") is False
    row = session.rows[("anne@example.com", "a" * 40)]
    assert row.thread_seen_count == 1
    assert row.status == "learning"
    assert row.last_thread_id == "t1"


def test_learns_signature_across_distinct_threads():
    """Threshold=3 default. Three distinct thread_ids → 3rd transitions to
    status='signature'; a 4th thread from the same sender skips upload."""
    session = _FakeSession()
    assert _call(session, thread_id="t1") is False  # learn, count=1
    assert _call(session, thread_id="t2") is False  # learn, count=2
    assert _call(session, thread_id="t3") is False  # learn, count=3 → signature
    row = session.rows[("anne@example.com", "a" * 40)]
    assert row.status == "signature"
    assert row.thread_seen_count == 3
    # Fourth thread: bytes get skipped.
    assert _call(session, thread_id="t4") is True


def test_recarry_within_one_thread_does_not_bump_count():
    """Same sha1, same sender, same thread_id seen 5 times → count stays at 1."""
    session = _FakeSession()
    for _ in range(5):
        assert _call(session, thread_id="t1") is False
    row = session.rows[("anne@example.com", "a" * 40)]
    assert row.thread_seen_count == 1
    assert row.status == "learning"


def test_different_sender_starts_own_counter():
    """Two senders sharing the literal same PNG file: each gets its own row,
    neither reaches threshold from the other's emails."""
    session = _FakeSession()
    _call(session, sender="anne@example.com", thread_id="t1")
    _call(session, sender="bob@example.com", thread_id="t1")
    assert session.rows[("anne@example.com", "a" * 40)].thread_seen_count == 1
    assert session.rows[("bob@example.com", "a" * 40)].thread_seen_count == 1


def test_different_sha1_same_filename_does_not_skip():
    """Sender's first email had a logo as `image001.png`. Later they send a
    REAL screenshot also named `image001.png` (different bytes). The
    screenshot uploads — dedup is on bytes, not filename."""
    session = _FakeSession()
    # Logo learned across 3 threads.
    for tid in ("t1", "t2", "t3"):
        _call(session, sha1="logo" + "0" * 36, thread_id=tid)
    assert session.rows[("anne@example.com", "logo" + "0" * 36)].status == "signature"
    # Real screenshot, same filename, different bytes.
    assert (
        _call(
            session,
            sha1="screenshot" + "0" * 30,
            filename="image001.png",
            thread_id="t4",
        )
        is False
    )


def test_non_image_is_never_learned():
    """A PDF that's byte-identical across 10 threads (e.g. an auto-attached
    legal disclaimer) still uploads. PDFs/DOCX/DWG are deliverables — we never
    auto-drop them."""
    session = _FakeSession()
    for tid in (f"t{i}" for i in range(10)):
        assert (
            _call(session, mime="application/pdf", filename="x.pdf", thread_id=tid)
            is False
        )
    assert session.rows == {}, "non-image must not write to the table"


def test_allowlisted_never_skips_and_does_not_bump():
    """User-allowlisted bytes always upload, even if count was already over
    threshold when they flipped the status."""
    session = _FakeSession()
    session.rows[("anne@example.com", "a" * 40)] = ContactSignatureImage(
        sender_email="anne@example.com",
        content_sha1="a" * 40,
        thread_seen_count=99,
        last_thread_id="old",
        first_filename="logo.png",
        status="allowlisted",
        first_seen_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert _call(session, thread_id="t-new") is False
    # Counter unchanged — the allowlist branch shouldn't bump.
    assert session.rows[("anne@example.com", "a" * 40)].thread_seen_count == 99
    assert session.rows[("anne@example.com", "a" * 40)].last_thread_id == "old"


def test_threshold_crossing_uploads_this_thread_skips_next():
    """The sighting that crosses the threshold completes the upload (we never
    yank a file mid-row), but a subsequent thread is skipped."""
    session = _FakeSession()
    _call(session, thread_id="t1")  # count=1
    _call(session, thread_id="t2")  # count=2
    # The transition sighting itself returns False (don't skip THIS one).
    assert _call(session, thread_id="t3") is False
    assert session.rows[("anne@example.com", "a" * 40)].status == "signature"
    # The next NEW thread is skipped.
    assert _call(session, thread_id="t4") is True


def test_sender_missing_does_not_record_and_uploads():
    """Forwarder lookup miss leaves attributed_sender empty. We can't
    attribute the bytes to a contact, so the helper is a no-op."""
    session = _FakeSession()
    assert _call(session, sender="", thread_id="t1") is False
    assert session.rows == {}


def test_sender_email_normalized_to_lowercase():
    """Helper lowercases the sender so `Anne@Example.com` and
    `anne@example.com` map to the same row."""
    session = _FakeSession()
    _call(session, sender="Anne@Example.COM", thread_id="t1")
    _call(session, sender="anne@example.com", thread_id="t2")
    assert ("anne@example.com", "a" * 40) in session.rows
    assert session.rows[("anne@example.com", "a" * 40)].thread_seen_count == 2


# --------------------------------------------------------------------------
# Integration: ordering of the re-carry skip vs the learn-helper.
#
# An in-thread re-carry (Bob quoting Anne's attachment on his reply) must
# NEVER reach the learn-helper — otherwise the bytes would be attributed
# to Bob and accumulate against him. The helper only ever sees the original
# sender's message. Drives `_upload_attachments` directly with a session
# that tracks signature rows.
# --------------------------------------------------------------------------

import gb_automations.sync.sync_thread as st
from gb_automations.clients.gmail import GmailAttachment, GmailMessage
from gb_automations.sync.sync_thread import AttachmentDecision


def _att(filename: str, size: int, attachment_id: str) -> GmailAttachment:
    return GmailAttachment(
        filename=filename,
        mime_type="image/png",
        size=size,
        attachment_id=attachment_id,
    )


def _msg(*, from_field: str, thread_id: str, attachments: list[GmailAttachment]) -> GmailMessage:
    return GmailMessage(
        message_id=f"m-{from_field}-{thread_id}",
        thread_id=thread_id,
        date=datetime(2026, 5, 29, tzinfo=timezone.utc),
        subject="Re: Tilbud",
        from_field=from_field,
        to_field="other@example.com",
        cc_field="",
        plain_body="",
        attachments=attachments,
        label_ids=[],
    )


def _drive_upload_attachments(
    *,
    session: _FakeSession,
    monkeypatch,
    parent_msg: GmailMessage,
    attributed_sender: str,
    decisions: list[AttachmentDecision],
    tracker,
) -> None:
    """Shim for `_upload_attachments` with Drive + Gmail bytes faked out.

    NAS off, single project folder. The shortcut path (sha1_by_namesize) is
    exercised when consecutive calls reuse the same (filename, size).
    """
    monkeypatch.setattr(
        st.gmail_client,
        "get_attachment_bytes",
        lambda user_email, message_id, attachment_id: attachment_id.encode(),
    )
    monkeypatch.setattr(
        st.drive_client,
        "upload_attachment",
        lambda user_email, folder_path, filename, mime, content: (
            f"https://drive/{filename}"
        ),
    )
    monkeypatch.setattr(st.settings, "sync_nas_folders", False)
    monkeypatch.setattr(st.settings, "attachments_folder_name", "Vedlegg")

    asyncio.run(
        st._upload_attachments(
            parent_msg=parent_msg,
            decisions=decisions,
            attributed_sender=attributed_sender,
            user_email="me@goldbox.no",
            session=session,
            thread_tracker=tracker,
            project_label_paths=["Prosjekt/2026/Acme"],
            project_page_ids=[],
            email_date=datetime(2026, 5, 29, tzinfo=timezone.utc),
            tags=[],
        )
    )


def test_recarry_does_not_bump_replier_counter_shortcut_hit(monkeypatch):
    """The common in-thread re-carry path: Bob's reply quotes Anne's
    attachment with identical (filename, size). The (filename, size) shortcut
    hits, content stays None, attached_this_pass drops it before the helper
    would run.

    Assertion: only Anne has a signature-learning row for those bytes.
    Bob has NO row — the helper never ran for his message.
    """
    session = _FakeSession()
    tracker = st.ThreadAttachmentTracker()
    att = _att("photo.jpg", 4096, "att-photo-1")
    sha1 = __import__("hashlib").sha1(b"att-photo-1").hexdigest()

    # Message 1 — Anne sends the file fresh.
    _drive_upload_attachments(
        session=session,
        monkeypatch=monkeypatch,
        parent_msg=_msg(from_field="anne@x.no", thread_id="t1", attachments=[att]),
        attributed_sender="anne@x.no",
        decisions=[AttachmentDecision(att, upload=True)],
        tracker=tracker,
    )
    # Reply — Bob quotes the same attachment (Outlook re-carries it).
    _drive_upload_attachments(
        session=session,
        monkeypatch=monkeypatch,
        parent_msg=_msg(from_field="bob@x.no", thread_id="t1", attachments=[att]),
        attributed_sender="bob@x.no",
        decisions=[AttachmentDecision(att, upload=True)],
        tracker=tracker,
    )

    assert ("anne@x.no", sha1) in session.rows
    assert ("bob@x.no", sha1) not in session.rows, (
        "re-carry must not create a learning row for the replier"
    )
    assert session.rows[("anne@x.no", sha1)].thread_seen_count == 1


def test_recarry_does_not_bump_replier_counter_shortcut_miss(monkeypatch):
    """The edge case: Bob's reply carries byte-identical bytes but the
    (filename, size) shortcut MISSES — different filename on the quoted MIME
    part. The fix being verified: the moved-up attached_this_pass check still
    catches it (sha1 matches what Anne attached) before the learn-helper runs.

    Without the fix, this would falsely bump (bob, sha1).
    """
    session = _FakeSession()
    tracker = st.ThreadAttachmentTracker()
    # Same attachment_id (so the fake yields identical bytes & sha1), but
    # different filenames — the (filename, size) shortcut keys on filename so
    # this misses, forcing the download path.
    anne_att = _att("photo.jpg", 4096, "att-photo-X")
    bob_att = _att("image001.png", 4096, "att-photo-X")  # same id => same bytes
    sha1 = __import__("hashlib").sha1(b"att-photo-X").hexdigest()

    _drive_upload_attachments(
        session=session,
        monkeypatch=monkeypatch,
        parent_msg=_msg(from_field="anne@x.no", thread_id="t1", attachments=[anne_att]),
        attributed_sender="anne@x.no",
        decisions=[AttachmentDecision(anne_att, upload=True)],
        tracker=tracker,
    )
    _drive_upload_attachments(
        session=session,
        monkeypatch=monkeypatch,
        parent_msg=_msg(from_field="bob@x.no", thread_id="t1", attachments=[bob_att]),
        attributed_sender="bob@x.no",
        decisions=[AttachmentDecision(bob_att, upload=True)],
        tracker=tracker,
    )

    assert ("anne@x.no", sha1) in session.rows
    assert ("bob@x.no", sha1) not in session.rows, (
        "shortcut-miss re-carry must still skip the learner via attached_this_pass"
    )
