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
from gb_automations.sync.sync_thread import _assemble_row_props, _projects_from_matches


def test_projects_from_matches_returns_all_sorted():
    # (notion_page_id, label_path) as returned by resolve_projects_for_labels,
    # deliberately out of order — must come back sorted by label path.
    matches = [
        ("page-b", "Prosjekt/2026/Bravo"),
        ("page-a", "Prosjekt/2026/Alpha"),
    ]

    names, ids, label_paths = _projects_from_matches(matches)

    assert names == ["Alpha", "Bravo"]
    assert ids == ["page-a", "page-b"]
    assert label_paths == ["Prosjekt/2026/Alpha", "Prosjekt/2026/Bravo"]


def test_projects_from_matches_empty():
    assert _projects_from_matches([]) == ([], [], [])


def test_projects_from_matches_single():
    names, ids, label_paths = _projects_from_matches(
        [("page-a", "Prosjekt/2026/1228_Metropolis_Versalen")]
    )

    # name is the leaf of the nested label path, used for human-friendly logs.
    assert names == ["1228_Metropolis_Versalen"]
    assert ids == ["page-a"]
    assert label_paths == ["Prosjekt/2026/1228_Metropolis_Versalen"]


def test_assemble_row_props_writes_multi_target_project_relation():
    # body shorter than 10 chars skips the LLM tagging call — keeps this test
    # a pure function check with no mocks.
    props, _tags = asyncio.run(
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
    props, _tags = asyncio.run(
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
