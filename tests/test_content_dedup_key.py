"""Tests for `_content_dedup_key` + `_normalize_for_dedup` in sync_thread.

The content-dedup key is the third (last) defense against duplicate Notion
rows when Gmail splits one conversation across multiple threads. Two
properties this layer must guarantee:

  1. **Same logical email re-quoted across N threads → same key.** Trivial
     drift (whitespace, case, `[image: …]` markers carried in one re-quote
     but not another) must NOT produce different keys, or duplicates leak
     through.

  2. **Two distinct short replies ("Takk!") from the same sender + project
     → different keys.** The `sent_at_minute` component ensures this: two
     genuinely-distinct emails always carry different timestamps.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from gb_automations.sync.sync_thread import (
    _content_dedup_key,
    _normalize_for_dedup,
    _sent_at_minute,
)

OSLO = timezone(timedelta(hours=1))
SENT_AT = datetime(2026, 6, 21, 14, 32, 17, tzinfo=OSLO)
PROJECTS = ["proj-abc"]


# ============================================================
# _normalize_for_dedup
# ============================================================


def test_normalize_lowercases():
    assert _normalize_for_dedup("Hei Tobias") == "hei tobias"


def test_normalize_collapses_whitespace_runs():
    assert _normalize_for_dedup("Hei\n\n\nTobias  \t  hvordan") == "hei tobias hvordan"


def test_normalize_strips_image_markers():
    body = "Hei Tobias\n[image: logo.png]\nMvh"
    assert _normalize_for_dedup(body) == "hei tobias mvh"


def test_normalize_strips_multiple_image_markers():
    body = "[image: a.png] hei [image: b.jpg] verden"
    assert _normalize_for_dedup(body) == "hei verden"


def test_normalize_empty_returns_empty():
    assert _normalize_for_dedup("") == ""
    assert _normalize_for_dedup("   \n\t  ") == ""


# ============================================================
# _sent_at_minute
# ============================================================


def test_sent_at_minute_converts_to_utc():
    # 2026-06-21 14:32 in Oslo (UTC+1) → 13:32 UTC
    assert _sent_at_minute(SENT_AT) == "2026-06-21T13:32"


def test_sent_at_minute_assumes_utc_when_naive():
    naive = datetime(2026, 6, 21, 14, 32, 17)
    assert _sent_at_minute(naive) == "2026-06-21T14:32"


def test_sent_at_minute_truncates_seconds():
    a = datetime(2026, 6, 21, 14, 32, 1, tzinfo=timezone.utc)
    b = datetime(2026, 6, 21, 14, 32, 59, tzinfo=timezone.utc)
    assert _sent_at_minute(a) == _sent_at_minute(b)


def test_sent_at_minute_none_returns_none():
    assert _sent_at_minute(None) is None


# ============================================================
# _content_dedup_key — collapse cases (same logical email)
# ============================================================


def test_same_body_same_minute_same_key():
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias\n\nKan du sende et tilbud?",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias\n\nKan du sende et tilbud?",
    )
    assert a is not None
    assert a == b


def test_whitespace_drift_doesnt_change_key():
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias\n\nKan du sende et tilbud?",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias  \n  Kan du sende et tilbud?   ",
    )
    assert a == b


def test_case_drift_doesnt_change_key():
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias, kan du sende et tilbud?",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="ANNE@x.com",
        sent_at=SENT_AT,
        body="HEI Tobias, kan du SENDE et tilbud?",
    )
    assert a == b


def test_image_marker_drift_doesnt_change_key():
    # The same logical email re-quoted across threads: one carry kept the
    # `[image: logo.png]` marker (this sync's attachment attribution pass
    # survived it), the other dropped it. Should still collapse.
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias\n[image: logo.png]\nMvh Anne",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Hei Tobias\n\nMvh Anne",
    )
    assert a == b


def test_seconds_drift_doesnt_change_key():
    # Gmail's internalDate vs regex-parsed "On X wrote:" can drift by a few
    # seconds. Truncating to the minute should absorb that.
    later = SENT_AT.replace(second=58)
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=later,
        body="Takk!",
    )
    assert a == b


# ============================================================
# _content_dedup_key — distinct cases (must NOT collapse)
# ============================================================


def test_different_timestamps_distinct_keys():
    # The whole point of adding sent_at: two genuinely-distinct "Takk!" replies
    # from the same sender + project must stay as two rows.
    later = SENT_AT + timedelta(hours=1)
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=later,
        body="Takk!",
    )
    assert a is not None
    assert b is not None
    assert a != b


def test_different_senders_distinct_keys():
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="bjorn@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    assert a != b


def test_different_projects_distinct_keys():
    # Per-project scoping is preserved by explicit user direction. Same content
    # under two different projects = two separate rows.
    a = _content_dedup_key(
        project_page_ids=["proj-abc"],
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    b = _content_dedup_key(
        project_page_ids=["proj-xyz"],
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    assert a != b


def test_different_bodies_distinct_keys():
    a = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="Takk!",
    )
    b = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="anne@x.com",
        sent_at=SENT_AT,
        body="OK",
    )
    assert a != b


# ============================================================
# _content_dedup_key — graceful fallback cases
# ============================================================


def test_no_project_returns_none():
    assert (
        _content_dedup_key(
            project_page_ids=[],
            from_email="anne@x.com",
            sent_at=SENT_AT,
            body="Takk!",
        )
        is None
    )


def test_missing_sent_at_returns_none():
    # Without a timestamp we can't distinguish two distinct short replies —
    # skip dedup rather than risk collapsing them.
    assert (
        _content_dedup_key(
            project_page_ids=PROJECTS,
            from_email="anne@x.com",
            sent_at=None,
            body="Takk!",
        )
        is None
    )


def test_empty_body_returns_none():
    assert (
        _content_dedup_key(
            project_page_ids=PROJECTS,
            from_email="anne@x.com",
            sent_at=SENT_AT,
            body="",
        )
        is None
    )


def test_whitespace_only_body_returns_none():
    assert (
        _content_dedup_key(
            project_page_ids=PROJECTS,
            from_email="anne@x.com",
            sent_at=SENT_AT,
            body="   \n\t  ",
        )
        is None
    )


def test_only_image_markers_body_returns_none():
    # After normalization a body that's only `[image: …]` markers is empty —
    # not a stable fingerprint, skip dedup.
    assert (
        _content_dedup_key(
            project_page_ids=PROJECTS,
            from_email="anne@x.com",
            sent_at=SENT_AT,
            body="[image: logo.png] [image: footer.png]",
        )
        is None
    )


def test_empty_sender_still_builds_key():
    # LLM-extracted name-only senders leave from_email empty. The empty-empty
    # match across two rows still legitimately means "same body, same project,
    # same minute, same nameless sender" — collapse it.
    key = _content_dedup_key(
        project_page_ids=PROJECTS,
        from_email="",
        sent_at=SENT_AT,
        body="Hei Tobias, kan du sende et tilbud?",
    )
    assert key is not None
    project, sender, minute, body_hash = key
    assert sender == ""
