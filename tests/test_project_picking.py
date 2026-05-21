"""Tests for project-label matching and the Project relation builder.

These pin the multi-project behavior: a thread with two matching project
labels must surface both, and the row's Project relation must carry both
target IDs. Without this, dual-labeled threads silently lose one project.
The integration paths (Notion PATCH on dedup hit) are exercised by manual
end-to-end verification — see the plan file.
"""

from __future__ import annotations

import asyncio

from gb_automations.config import EMAILS_PROPS
from gb_automations.sync.sync_thread import _assemble_row_props, _pick_projects


def test_pick_projects_returns_all_matches_sorted():
    project_map = {
        "Prosjekt/2026/Bravo": {"id": "page-b", "title": "Bravo", "created_time": ""},
        "Prosjekt/2026/Alpha": {"id": "page-a", "title": "Alpha", "created_time": ""},
        "Prosjekt/2026/Other": {"id": "page-o", "title": "Other", "created_time": ""},
    }
    labels = {"INBOX", "Prosjekt/2026/Bravo", "Prosjekt/2026/Alpha"}

    titles, ids, label_paths = _pick_projects(labels, project_map)

    # Sorted by full label path, so order is deterministic across re-syncs even
    # when Gmail returns labels in a different order.
    assert titles == ["Alpha", "Bravo"]
    assert ids == ["page-a", "page-b"]
    assert label_paths == ["Prosjekt/2026/Alpha", "Prosjekt/2026/Bravo"]


def test_pick_projects_returns_empty_when_no_label_matches():
    project_map = {
        "Prosjekt/2026/Alpha": {"id": "page-a", "title": "Alpha", "created_time": ""},
    }
    labels = {"INBOX", "SENT", "Prosjekt/2025/Bravo"}  # different year, no match

    titles, ids, label_paths = _pick_projects(labels, project_map)

    assert titles == []
    assert ids == []
    assert label_paths == []


def test_pick_projects_single_match_returns_one_element_list():
    project_map = {
        "Prosjekt/2026/Alpha": {"id": "page-a", "title": "Alpha", "created_time": ""},
        "Prosjekt/2026/Bravo": {"id": "page-b", "title": "Bravo", "created_time": ""},
    }
    labels = {"Prosjekt/2026/Alpha"}

    titles, ids, label_paths = _pick_projects(labels, project_map)

    assert titles == ["Alpha"]
    assert ids == ["page-a"]
    assert label_paths == ["Prosjekt/2026/Alpha"]


def test_assemble_row_props_writes_multi_target_project_relation():
    # body shorter than 10 chars skips the LLM tagging call — keeps this test
    # a pure function check with no mocks.
    props = asyncio.run(
        _assemble_row_props(
            emails_db_props={EMAILS_PROPS["project"]},
            subject="hi",
            thread_id="t1",
            message_id="m1",
            project_page_ids=["page-a", "page-b"],
            from_email="",
            to_emails=[],
            cc_emails=[],
            contact_ids={},
            date_iso="2026-05-19T00:00:00+00:00",
            body="",
        )
    )

    relation = props[EMAILS_PROPS["project"]]["relation"]
    assert relation == [{"id": "page-a"}, {"id": "page-b"}]


def test_assemble_row_props_writes_single_relation_for_single_project():
    props = asyncio.run(
        _assemble_row_props(
            emails_db_props={EMAILS_PROPS["project"]},
            subject="hi",
            thread_id="t1",
            message_id="m1",
            project_page_ids=["page-a"],
            from_email="",
            to_emails=[],
            cc_emails=[],
            contact_ids={},
            date_iso="2026-05-19T00:00:00+00:00",
            body="",
        )
    )

    assert props[EMAILS_PROPS["project"]]["relation"] == [{"id": "page-a"}]
