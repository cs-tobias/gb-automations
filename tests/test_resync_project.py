"""Unit tests for the project-resync engine's pure decision logic.

Two things worth pinning without a live Gmail/Notion/Postgres:
1. `_enumerate_threads` dedupes a thread that's labeled in multiple mailboxes
   down to ONE owning user (deterministic by sorted email), so the thread syncs
   exactly once.
2. `dry_run=True` mutates nothing — no archive, no cache delete, no sync — even
   though it still enumerates and reports.
"""

from __future__ import annotations

import asyncio

import gb_automations.sync.resync_project as rp


def test_enumerate_threads_dedupes_to_one_owner(monkeypatch):
    # alice and bob both have the project label; threads t1/t2 appear in both
    # mailboxes, t3 only in bob's. Each thread must map to a single owner, and
    # the shared ones go to the alphabetically-first user (alice).
    def fake_list(user_email, label_name, max_results, *, paginate):
        assert paginate is True  # the engine must request the full list
        by_user = {
            "alice@x.no": [{"id": "t1"}, {"id": "t2"}],
            "bob@x.no": [{"id": "t1"}, {"id": "t2"}, {"id": "t3"}],
        }
        return by_user[user_email]

    monkeypatch.setattr(rp.gmail_client, "list_threads_with_label", fake_list)

    labels = [("bob@x.no", "Projects/2026/Acme"), ("alice@x.no", "Projects/2026/Acme")]
    owner = asyncio.run(rp._enumerate_threads(labels))

    assert owner == {"t1": "alice@x.no", "t2": "alice@x.no", "t3": "bob@x.no"}


def test_dry_run_mutates_nothing(monkeypatch):
    archived: list[str] = []
    cleared: list[str] = []

    async def fake_resolve(project_page_id, only_user):
        return [("alice@x.no", "Projects/2026/Acme")]

    async def fake_enumerate(labels):
        return {"t1": "alice@x.no", "t2": "alice@x.no"}

    async def fake_page_ids(thread_ids):
        return ["page-1", "page-2"]

    async def fake_get_page(page_id):
        return {}

    async def boom_archive(page_ids):
        archived.extend(page_ids)
        return (len(page_ids), 0)

    async def boom_clear(thread_ids, hard):
        cleared.extend(thread_ids)
        return (len(thread_ids), 0)

    async def boom_sync(user_email, thread_id):  # pragma: no cover - must not run
        raise AssertionError("sync_thread must not run during a dry run")

    monkeypatch.setattr(rp, "_resolve_labels", fake_resolve)
    monkeypatch.setattr(rp, "_enumerate_threads", fake_enumerate)
    monkeypatch.setattr(rp, "_page_ids_for_threads", fake_page_ids)
    monkeypatch.setattr(rp, "_archive_pages", boom_archive)
    monkeypatch.setattr(rp, "_clear_local_cache", boom_clear)
    monkeypatch.setattr(rp, "sync_thread", boom_sync)
    monkeypatch.setattr(rp.notion_client, "extract_page_title", lambda page: "Acme")
    monkeypatch.setattr(rp.notion_client, "get_page", fake_get_page)

    result = asyncio.run(rp.resync_project("proj-123", dry_run=True))

    assert result.dry_run is True
    assert result.thread_ids == ["t1", "t2"]
    assert result.page_ids_to_archive == ["page-1", "page-2"]
    assert result.pages_archived == 0
    assert result.email_rows_deleted == 0
    assert archived == []  # archive never called
    assert cleared == []  # cache never cleared


def _projects():
    return [
        rp.ProjectRef(page_id="id-kj8", name="Projects/2026/1232_Eiendomsspar_KJ8"),
        rp.ProjectRef(page_id="id-toi", name="Projects/2026/1234_fredriksborg_Toeihuset"),
        rp.ProjectRef(page_id="id-kj9", name="Projects/2026/1240_Eiendomsspar_KJ9"),
    ]


def test_resolve_by_exact_page_id(monkeypatch):
    async def fake_list():
        return _projects()

    monkeypatch.setattr(rp, "list_projects", fake_list)
    matches = asyncio.run(rp.resolve_project("id-toi"))
    assert [m.page_id for m in matches] == ["id-toi"]


def test_resolve_by_unique_name_substring(monkeypatch):
    async def fake_list():
        return _projects()

    monkeypatch.setattr(rp, "list_projects", fake_list)
    matches = asyncio.run(rp.resolve_project("toeihuset"))  # case-insensitive
    assert [m.page_id for m in matches] == ["id-toi"]


def test_resolve_ambiguous_substring_returns_all(monkeypatch):
    async def fake_list():
        return _projects()

    monkeypatch.setattr(rp, "list_projects", fake_list)
    matches = asyncio.run(rp.resolve_project("Eiendomsspar"))
    assert {m.page_id for m in matches} == {"id-kj8", "id-kj9"}


def test_resolve_no_match_is_empty(monkeypatch):
    async def fake_list():
        return _projects()

    monkeypatch.setattr(rp, "list_projects", fake_list)
    assert asyncio.run(rp.resolve_project("nonexistent")) == []


def test_double_fire_is_skipped(monkeypatch):
    # A second concurrent resync of the same project short-circuits.
    rp._in_flight.add("proj-busy")
    try:
        result = asyncio.run(rp.resync_project("proj-busy"))
    finally:
        rp._in_flight.discard("proj-busy")
    assert result.already_running is True
    assert result.rows_created == 0
