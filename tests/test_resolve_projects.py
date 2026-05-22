"""Tests for resolve_projects_for_labels — the local ProjectLabel lookup that
replaced the per-thread Notion project-catalog fetch.

Pins: (1) empty label set short-circuits with no DB round-trip; (2) the lookup
returns (notion_page_id, current_name) tuples; (3) it filters on user_email AND
gmail_label_id (the composite-index hot path), keyed on label *id* so it stays
correct across project renames.
"""

from __future__ import annotations

import asyncio

import gb_automations.sync.watches as watches


def test_empty_labels_short_circuits_without_db(monkeypatch):
    # If a thread carries no labels, we must not even open a session.
    def _boom():
        raise AssertionError("SessionLocal should not be called for empty labels")

    monkeypatch.setattr(watches, "SessionLocal", _boom)

    assert asyncio.run(watches.resolve_projects_for_labels("a@x.no", set())) == []


def test_returns_page_id_and_name_tuples(monkeypatch):
    captured = {}

    class _Result:
        def all(self):
            return [
                ("page-a", "Prosjekt/2026/Alpha"),
                ("page-b", "Prosjekt/2026/Bravo"),
            ]

    class _Session:
        async def execute(self, stmt):
            captured["stmt"] = stmt
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(watches, "SessionLocal", lambda: _Session())

    out = asyncio.run(
        watches.resolve_projects_for_labels("a@x.no", {"Label_1", "Label_2"})
    )

    assert out == [
        ("page-a", "Prosjekt/2026/Alpha"),
        ("page-b", "Prosjekt/2026/Bravo"),
    ]
    # The query must filter by user_email AND gmail_label_id — sanity-check the
    # compiled SQL mentions both columns (the composite-index hot path).
    sql = str(captured["stmt"]).lower()
    assert "user_email" in sql and "gmail_label_id" in sql
