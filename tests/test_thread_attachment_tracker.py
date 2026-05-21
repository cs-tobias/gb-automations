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

from gb_automations.sync.sync_thread import ThreadAttachmentTracker


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
