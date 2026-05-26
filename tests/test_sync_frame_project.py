"""Unit tests for sync_frame.sync_frame_project.

Pins the behaviors the Phase 1 Frame mirror must hold without hitting Postgres,
Frame.io, or Notion:

  1. SYNC_FRAME=false / no root project → skipped (no API calls).
  2. No Notion title → skipped with note (no API calls).
  3. First sync → create_folder runs against the resolved year folder + URL is
     patched back onto the Notion page.
  4. Rename branch → rename_folder runs; create_folder does NOT.
  5. Self-heal → cached folder id that 404s is evicted and a fresh create runs.
  6. Notion writeback failure does not flip the result to "failed" (the folder
     exists; the next run retries the patch).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import gb_automations.sync.sync_frame as sf
from gb_automations.clients import frame as frame_client


# --------------------------------------------------------------------------
# Fakes for SessionLocal + cached row
# --------------------------------------------------------------------------


@dataclass
class _FakeProjectRow:
    notion_page_id: str
    frame_folder_id: str = "old-folder-id"
    frame_parent_id: str = "year-folder-id"
    current_name: str = "Old name"
    current_year: str = "2026"
    frame_url: str = "https://next.frame.io/project/p/view/old-folder-id"


class _FakeSession:
    """Minimal async-session stub covering the calls sync_frame makes:
    get/add/merge/delete/flush/commit, plus the async context-manager."""

    def __init__(self, get_returns=None):
        self._get_returns = get_returns
        self.added: list = []
        self.merged: list = []
        self.deleted: list = []
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, _model, _pk):
        return self._get_returns

    def add(self, obj) -> None:
        self.added.append(obj)

    async def merge(self, obj):
        self.merged.append(obj)
        return obj

    async def delete(self, obj) -> None:
        self.deleted.append(obj)
        # Mimic SQLAlchemy: a subsequent .get() on the same pk should return None
        self._get_returns = None

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr(sf, "SessionLocal", lambda: session)


def _patch_settings(monkeypatch, *, sync_frame=True, root_project="rootP", placeholder="http://x/p.png"):
    monkeypatch.setattr(sf.settings, "sync_frame", sync_frame)
    monkeypatch.setattr(sf.settings, "frame_root_project_id", root_project)
    monkeypatch.setattr(sf.settings, "frame_placeholder_url", placeholder)


def _aval(value):
    async def _f(*a, **k):
        return value
    return _f


def _patch_notion(monkeypatch, *, title="Acme", created="2026-01-01T00:00:00Z"):
    monkeypatch.setattr(
        sf.notion_client, "get_page", _aval({"created_time": created})
    )
    monkeypatch.setattr(sf.notion_client, "extract_page_title", lambda page: title)


def _reset_caches():
    sf._year_folder_cache.clear()
    sf._discipline_folder_cache.clear()


# --------------------------------------------------------------------------
# Skipped branches
# --------------------------------------------------------------------------


def test_skipped_when_sync_frame_off(monkeypatch):
    _patch_settings(monkeypatch, sync_frame=False)
    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "skipped"
    assert result.note == "SYNC_FRAME=false"


def test_skipped_when_root_project_unset(monkeypatch):
    _patch_settings(monkeypatch, root_project="")
    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "skipped"
    assert result.note == "FRAME_ROOT_PROJECT_ID not set"


def test_skipped_when_no_title(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title=None)
    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "skipped"
    assert result.note == "no title yet"


# --------------------------------------------------------------------------
# Create branch
# --------------------------------------------------------------------------


def test_first_sync_creates_folder_and_writes_url(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    calls: list[tuple] = []

    async def fake_ensure_year(year):
        calls.append(("ensure_year", year))
        return "yearF"

    async def fake_create_folder(parent, name):
        calls.append(("create_folder", parent, name))
        return {"id": "newF"}

    async def fake_get_folder(_):
        raise AssertionError("self-heal path must not run on fresh sync")

    async def fake_set_url(page_id, url):
        calls.append(("set_url", page_id, url))

    monkeypatch.setattr(sf, "_ensure_year_folder", fake_ensure_year)
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "get_folder", fake_get_folder)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", fake_set_url)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "created"
    assert result.frame_folder_id == "newF"
    assert ("ensure_year", "2026") in calls
    assert ("create_folder", "yearF", "Acme") in calls
    assert ("set_url", "p1", result.frame_url) in calls
    assert len(session.added) == 1
    assert session.commits == 1


# --------------------------------------------------------------------------
# Rename branch
# --------------------------------------------------------------------------


def test_rename_calls_rename_folder_only(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="New Name")

    create_calls = 0

    async def fake_create_folder(*a, **k):
        nonlocal create_calls
        create_calls += 1
        return {"id": "shouldnt-be-called"}

    rename_calls: list[tuple] = []

    async def fake_rename_folder(folder_id, new_name):
        rename_calls.append((folder_id, new_name))
        return {"id": folder_id, "name": new_name}

    async def fake_get_folder(folder_id):
        return {"id": folder_id}  # live → not stale

    async def fake_set_url(*a, **k):
        return None

    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "rename_folder", fake_rename_folder)
    monkeypatch.setattr(sf.frame_client, "get_folder", fake_get_folder)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", fake_set_url)

    row = _FakeProjectRow(notion_page_id="p1", current_name="Old Name")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "renamed"
    assert rename_calls == [(row.frame_folder_id, "New Name")]
    assert create_calls == 0
    assert row.current_name == "New Name"  # mutation persisted via merge


def test_unchanged_when_name_matches(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="SameName")

    async def fake_get_folder(folder_id):
        return {"id": folder_id}

    monkeypatch.setattr(sf.frame_client, "get_folder", fake_get_folder)
    monkeypatch.setattr(
        sf.frame_client, "create_folder", _aval({"id": "nope"})
    )
    monkeypatch.setattr(
        sf.frame_client, "rename_folder", _aval({"id": "nope"})
    )
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    row = _FakeProjectRow(notion_page_id="p1", current_name="SameName")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "unchanged"
    assert result.frame_folder_id == row.frame_folder_id


# --------------------------------------------------------------------------
# Self-heal branch
# --------------------------------------------------------------------------


def _make_404():
    import httpx
    response = httpx.Response(
        404, request=httpx.Request("GET", "http://x"), json={"error": "gone"}
    )
    return frame_client.FrameAPIError(response)


def test_self_heal_on_stale_folder_id(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    async def fake_get_folder(folder_id):
        raise _make_404()

    create_calls: list[tuple] = []

    async def fake_ensure_year(year):
        return "yearF"

    async def fake_create_folder(parent, name):
        create_calls.append((parent, name))
        return {"id": "fresh"}

    monkeypatch.setattr(sf.frame_client, "get_folder", fake_get_folder)
    monkeypatch.setattr(sf, "_ensure_year_folder", fake_ensure_year)
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    row = _FakeProjectRow(notion_page_id="p1", current_name="Acme")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "created"
    assert result.frame_folder_id == "fresh"
    assert create_calls == [("yearF", "Acme")]
    assert session.deleted == [row]


# --------------------------------------------------------------------------
# Notion writeback resilience
# --------------------------------------------------------------------------


def test_notion_writeback_failure_does_not_fail_the_task(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    monkeypatch.setattr(sf, "_ensure_year_folder", _aval("yearF"))
    monkeypatch.setattr(sf.frame_client, "create_folder", _aval({"id": "F"}))
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "F"}))

    async def boom(*a, **k):
        raise RuntimeError("Notion is down")

    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", boom)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "created"  # the folder was made; URL retry next time
    assert result.frame_folder_id == "F"


# --------------------------------------------------------------------------
# Failure path
# --------------------------------------------------------------------------


def test_frame_api_failure_marks_action_failed(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    async def boom(*a, **k):
        raise RuntimeError("frame down")

    monkeypatch.setattr(sf, "_ensure_year_folder", boom)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "failed"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
