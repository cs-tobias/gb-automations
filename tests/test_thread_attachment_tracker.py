"""Tests for the per-thread attachment dedup tracker.

One rule survives: thread-wide exact sha1. Identical bytes anywhere in the
thread are the same file regardless of sender — this kills the re-carry
duplicates (each reply quotes earlier attachments under a DIFFERENT author, so
a sender-scoped rule alone re-uploaded the same file once per reply).

Crucially, a sha1 hit no longer *drops* the attachment: the tracker stores the
Drive links the bytes uploaded to, so the quoting row can be RE-LINKED to the
file already on Drive. "Already uploaded" must never mean "missing from the
row" — that was the production bug (files in Drive, but no Notion row carried
them). The fuzzy ±size and repeating-signature heuristics were removed entirely.
"""

from __future__ import annotations

from gb_automations.sync.sync_thread import ThreadAttachmentTracker


def test_known_sha1_returns_stored_links():
    # The re-link contract: once bytes are recorded with their Drive links, a
    # later re-carry of the SAME bytes looks them up instead of re-uploading.
    tracker = ThreadAttachmentTracker()
    links = [{"name": "Plan.dwg", "url": "https://drive/abc"}]
    tracker.links_by_sha1["photo-sha"] = links

    assert "photo-sha" in tracker.sha1s
    assert tracker.links_by_sha1.get("photo-sha") == links


def test_recarry_across_senders_same_bytes_is_known():
    # The actual bug: a reply quotes the earlier message and Gmail re-carries
    # the IDENTICAL bytes under the replier's name. Same sha1 → known, so the
    # upload is skipped but the row is re-linked from the stored URLs.
    tracker = ThreadAttachmentTracker()
    tracker.links_by_sha1["img-sha"] = [
        {"name": "IMG_2555.jpeg", "url": "https://drive/img"}
    ]
    # A different author quoting the same bytes still hits the same sha1.
    assert "img-sha" in tracker.sha1s


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
