"""Unit tests for sync_nas.sync_nas_folder + the container→Windows path conversion.

Pin the behaviors the new nas_folder_sync task type must hold without hitting
Postgres, the NAS share, or Notion:

  1. Container `/mnt/nas/Prosjekt/...` path converts to
     `X:\\gb-nas-test\\Prosjekt\\...` when NAS_HOST_PATH is set; None otherwise.
  2. Disabled (sync_nas_folders=false or no NAS_PROJECTS_ROOT) → skipped, no
     filesystem calls.
  3. Notion title missing → skipped.
  4. NAS unavailable (mount down) → action="failed" so the queue parks it.
  5. First sync calls ensure_project_folders + writes the Windows-style URL
     back to Notion.
  6. Rename branch calls rename_project_folder.
  7. Notion writeback failure does not flip the task to failed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import pytest

import gb_automations.sync.sync_nas as sn


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class _FakeProjectFolderRow:
    notion_page_id: str
    current_name: str = "Old name"
    current_path: str = "/mnt/nas/Prosjekt/2026/Old name"


class _FakeSession:
    def __init__(self, get_returns=None):
        self._get_returns = get_returns
        self.merged: list = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, _model, _pk):
        return self._get_returns

    async def merge(self, obj):
        self.merged.append(obj)
        return obj

    async def commit(self) -> None:
        self.commits += 1


def _aval(value):
    async def _f(*a, **k):
        return value

    return _f


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr(sn, "SessionLocal", lambda: session)


def _patch_settings(
    monkeypatch,
    *,
    sync=True,
    root="/mnt/nas/Prosjekt",
    host_path="X:\\gb-nas-test",
):
    monkeypatch.setattr(sn.settings, "sync_nas_folders", sync)
    monkeypatch.setattr(sn.settings, "nas_projects_root", root)
    monkeypatch.setattr(sn.settings, "nas_host_path", host_path)


def _patch_notion(monkeypatch, *, title="Acme", created="2026-01-01T00:00:00Z"):
    monkeypatch.setattr(
        sn.notion_client, "get_page", _aval({"created_time": created})
    )
    monkeypatch.setattr(sn.notion_client, "extract_page_title", lambda page: title)
    monkeypatch.setattr(sn.notion_client, "oppgaver_for_project", _aval([]))


# --------------------------------------------------------------------------
# Path conversion (pure function, no I/O)
# --------------------------------------------------------------------------


def test_display_path_converts_posix_root_to_windows(monkeypatch):
    _patch_settings(monkeypatch)
    out = sn._to_display_path(Path("/mnt/nas/Prosjekt/2026/1300_X"))
    assert out == "X:\\gb-nas-test\\Prosjekt\\2026\\1300_X"


def test_display_path_handles_name_with_underscores(monkeypatch):
    _patch_settings(monkeypatch)
    out = sn._to_display_path(Path("/mnt/nas/Prosjekt/2026/1300_NewProject_Tuesday"))
    assert out == "X:\\gb-nas-test\\Prosjekt\\2026\\1300_NewProject_Tuesday"


def test_display_path_returns_none_when_host_path_unset(monkeypatch):
    _patch_settings(monkeypatch, host_path="")
    assert sn._to_display_path(Path("/mnt/nas/Prosjekt/2026/X")) is None


def test_display_path_returns_none_when_outside_container_mount(monkeypatch):
    """Defensive: a target that isn't under /mnt/nas means our cache state is
    broken — return None rather than store a misleading path."""
    _patch_settings(monkeypatch)
    assert sn._to_display_path(Path("/tmp/somewhere/else")) is None


# --------------------------------------------------------------------------
# Skipped branches
# --------------------------------------------------------------------------


def test_skipped_when_sync_nas_off(monkeypatch):
    _patch_settings(monkeypatch, sync=False)
    result = asyncio.run(sn.sync_nas_folder("p1"))
    assert result.action == "skipped"
    assert result.note and "sync_nas_folders" in result.note


def test_skipped_when_nas_root_unset(monkeypatch):
    _patch_settings(monkeypatch, root="")
    result = asyncio.run(sn.sync_nas_folder("p1"))
    assert result.action == "skipped"


def test_skipped_when_no_title(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title=None)
    monkeypatch.setattr(sn.nas_client, "nas_available", lambda: True)
    result = asyncio.run(sn.sync_nas_folder("p1"))
    assert result.action == "skipped"
    assert result.note == "no title yet"


# --------------------------------------------------------------------------
# Failure: NAS share unavailable
# --------------------------------------------------------------------------


def test_failed_when_nas_unavailable(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch)
    monkeypatch.setattr(sn.nas_client, "nas_available", lambda: False)

    result = asyncio.run(sn.sync_nas_folder("p1"))
    assert result.action == "failed"
    assert result.note == "NAS share unavailable"


# --------------------------------------------------------------------------
# First sync: create + URL writeback
# --------------------------------------------------------------------------


def test_first_sync_creates_and_writes_url(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    target = Path("/mnt/nas/Prosjekt/2026/Acme")
    calls: list = []

    def fake_ensure(title, created_time, disciplines):
        calls.append(("ensure", title, list(disciplines or [])))
        return target

    monkeypatch.setattr(sn.nas_client, "nas_available", lambda: True)
    monkeypatch.setattr(sn.nas_client, "ensure_project_folders", fake_ensure)

    set_url_calls: list = []

    async def fake_set_url(page_id, url):
        set_url_calls.append((page_id, url))

    monkeypatch.setattr(sn.notion_client, "set_project_nas_url", fake_set_url)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sn.sync_nas_folder("p1"))

    assert result.action == "created"
    assert result.target_path == str(target)
    assert result.display_path == "X:\\gb-nas-test\\Prosjekt\\2026\\Acme"
    assert len(set_url_calls) == 1
    assert set_url_calls[0] == ("p1", "X:\\gb-nas-test\\Prosjekt\\2026\\Acme")
    assert calls and calls[0][0] == "ensure"
    assert session.commits == 1


def test_rename_branch_calls_rename(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="New name")

    rename_calls: list = []
    ensure_calls = 0

    def fake_rename(old, new, ct, disciplines):
        rename_calls.append((old, new))
        return Path("/mnt/nas/Prosjekt/2026/New name")

    def fake_ensure(*a, **k):
        nonlocal ensure_calls
        ensure_calls += 1
        return Path("/mnt/nas/Prosjekt/2026/New name")

    monkeypatch.setattr(sn.nas_client, "nas_available", lambda: True)
    monkeypatch.setattr(sn.nas_client, "rename_project_folder", fake_rename)
    monkeypatch.setattr(sn.nas_client, "ensure_project_folders", fake_ensure)
    monkeypatch.setattr(sn.notion_client, "set_project_nas_url", _aval(None))

    row = _FakeProjectFolderRow(notion_page_id="p1", current_name="Old name")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sn.sync_nas_folder("p1"))
    assert result.action == "renamed"
    assert rename_calls == [("Old name", "New name")]
    assert ensure_calls == 0


# --------------------------------------------------------------------------
# Notion writeback resilience
# --------------------------------------------------------------------------


def test_notion_writeback_failure_does_not_fail_the_task(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch)

    monkeypatch.setattr(sn.nas_client, "nas_available", lambda: True)
    monkeypatch.setattr(
        sn.nas_client,
        "ensure_project_folders",
        lambda t, ct, disc: Path("/mnt/nas/Prosjekt/2026/Acme"),
    )

    async def boom(*a, **k):
        raise RuntimeError("Notion is down")

    monkeypatch.setattr(sn.notion_client, "set_project_nas_url", boom)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sn.sync_nas_folder("p1"))
    # Folder was created; URL retry will happen on the next sync.
    assert result.action == "created"
    assert result.display_path is None  # writeback didn't land


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
