"""Tests for the per-thread attachment dedup tracker.

Two rules:
1. Thread-wide exact sha1 — identical bytes anywhere in the thread are the
   same image regardless of sender. This kills the re-carry duplicates:
   each reply quotes earlier attachments under a DIFFERENT author, so the
   sender-scoped rule alone re-uploaded the same photo once per reply.
2. (sender, filename) + ±10 KB — the fuzzy fallback for the same image Gmail
   re-wrapped between messages so its sha1 shifted; scoped to one sender so
   two participants sharing `screenshot.png` aren't collapsed.
"""

from __future__ import annotations

from gb_automations.sync.sync_thread import (
    SIZE_TOLERANCE_BYTES,
    ThreadAttachmentTracker,
)


def _is_dup(
    tracker: ThreadAttachmentTracker,
    *,
    sender: str,
    filename: str,
    size: int,
    sha1: str,
) -> bool:
    # Mirrors the predicate in _upload_attachments: global sha1 first, then
    # the sender+filename ±size fallback. Kept as a pure function so the test
    # exercises the same logic without the full async upload coroutine.
    if sha1 in tracker.sha1s:
        return True
    prior = tracker.seen.get((sender, filename), [])
    return any(
        abs(prior_size - size) <= SIZE_TOLERANCE_BYTES for prior_size, _ in prior
    )


def _record(
    tracker: ThreadAttachmentTracker,
    *,
    sender: str,
    filename: str,
    size: int,
    sha1: str,
) -> None:
    tracker.sha1s.add(sha1)
    tracker.seen.setdefault((sender, filename), []).append((size, sha1))


def test_repeated_tiff_signature_with_size_drift_is_duplicate():
    # Real values from the bug report: PastedGraphic-7.tiff from rino@.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="rino@klatredigital.no",
        filename="PastedGraphic-7.tiff",
        size=6451,
        sha1="aaa",
    )
    assert _is_dup(
        tracker,
        sender="rino@klatredigital.no",
        filename="PastedGraphic-7.tiff",
        size=6553,  # 102 B drift — well within ±10 KB
        sha1="bbb",  # different bytes, but caught by size-window
    )


def test_exact_byte_recarry_is_duplicate():
    # Legacy path: a reply that quotes an earlier message re-carries the
    # same MIME part. Identical size and identical sha1 — must skip.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="rino@klatredigital.no",
        filename="tilbud.pdf",
        size=42000,
        sha1="abc123",
    )
    assert _is_dup(
        tracker,
        sender="rino@klatredigital.no",
        filename="tilbud.pdf",
        size=42000,
        sha1="abc123",
    )


def test_different_sender_same_filename_different_bytes_is_not_duplicate():
    # Two participants both happen to attach "screenshot.png" — different
    # bytes (different sha1), so both should upload.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="alice@example.com",
        filename="screenshot.png",
        size=50_000,
        sha1="alice-sha",
    )
    assert not _is_dup(
        tracker,
        sender="bob@example.com",
        filename="screenshot.png",
        size=50_000,
        sha1="bob-sha",
    )


def test_recarry_across_senders_same_bytes_is_duplicate():
    # The actual bug: a reply quotes the earlier message and Gmail re-carries
    # the IDENTICAL image bytes under the replier's name. Same sha1, different
    # sender (and even a different Gmail-renumbered filename) — must skip so
    # the photo doesn't re-upload once per reply in the thread.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="lasse@kongssenteret.no",
        filename="IMG_2555.jpeg",
        size=2_698_240,
        sha1="photo-sha",
    )
    assert _is_dup(
        tracker,
        sender="hello@motionindex.io",  # different author, quoted reply
        filename="IMG_2555.jpeg",
        size=2_698_240,
        sha1="photo-sha",  # identical bytes
    )


def test_same_sender_different_filename_is_not_duplicate():
    # Two genuinely different attachments (distinct bytes) from one sender —
    # both upload even though the size happens to match. Filename-scoping on
    # the fuzzy rule keeps them apart.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="rino@klatredigital.no",
        filename="PastedGraphic-7.tiff",
        size=6451,
        sha1="aaa",
    )
    assert not _is_dup(
        tracker,
        sender="rino@klatredigital.no",
        filename="PastedGraphic-8.tiff",
        size=6451,
        sha1="bbb",  # different bytes — a different image
    )


def test_same_sender_filename_but_size_outside_window_is_not_duplicate():
    # A real re-send of a meaningfully different version (e.g. revisjon-3.pdf
    # → revisjon-3.pdf with new content) shifts size by far more than 10 KB.
    # The team wants both versions uploaded.
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="designer@studio.no",
        filename="revisjon-3.pdf",
        size=120_000,
        sha1="v1-sha",
    )
    assert not _is_dup(
        tracker,
        sender="designer@studio.no",
        filename="revisjon-3.pdf",
        size=145_000,  # 25 KB heavier — clearly a different version
        sha1="v2-sha",
    )


def test_size_exactly_at_tolerance_boundary_is_duplicate():
    # Boundary check: a delta exactly equal to SIZE_TOLERANCE_BYTES must
    # still count as a duplicate (the `<=` in the predicate).
    tracker = ThreadAttachmentTracker()
    _record(
        tracker,
        sender="rino@klatredigital.no",
        filename="signature.tiff",
        size=10_000,
        sha1="aaa",
    )
    assert _is_dup(
        tracker,
        sender="rino@klatredigital.no",
        filename="signature.tiff",
        size=10_000 + SIZE_TOLERANCE_BYTES,
        sha1="bbb",
    )
