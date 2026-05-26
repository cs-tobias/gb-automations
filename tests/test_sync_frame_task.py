"""Unit tests for sync_frame.sync_frame_task.

Phase 1 behaviors pinned without DB / Frame.io / Notion:

  1. Skip branches (settings off, no title, no project relation, no discipline,
     unrecognized discipline).
  2. First sync creates task folder + placeholder file + writes URL back.
  3. Rename preserves the placeholder file id (Phase 2 will need it stable).
  4. Discipline change is logged as a warning and renames in place (re-parenting
     deferred).
  5. Self-heal: stale cached folder id (404) → evict → re-create.
  6. Recursive provisioning: a missing parent project is created on demand so
     the queue can drain task tasks before project tasks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import gb_automations.sync.sync_frame as sf
from gb_automations.clients import frame as frame_client


@dataclass
class _FakeProjectRow:
    # frame_folder_id here is the Project's root_folder_id (the parent
    # passed to _ensure_discipline_folder). Same column name as before the
    # workspace-Project refactor, slightly different semantics.
    notion_page_id: str
    frame_project_id: str = "projID"
    frame_folder_id: str = "projF"
    current_name: str = "Acme"
    frame_url: str = "u"


@dataclass
class _FakeTaskRow:
    notion_page_id: str
    project_page_id: str = "p1"
    frame_folder_id: str = "taskF"
    frame_placeholder_file_id: str = "fileF"
    current_name: str = "Old"
    current_discipline: str = "Eksteriør"
    frame_url: str = "u"


class _FakeSession:
    def __init__(self, by_pk: dict | None = None):
        self._by_pk = by_pk or {}
        self.added: list = []
        self.merged: list = []
        self.deleted: list = []
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, model, pk):
        return self._by_pk.get((model.__name__, pk))

    def add(self, obj):
        self.added.append(obj)

    async def merge(self, obj):
        self.merged.append(obj)
        return obj

    async def delete(self, obj) -> None:
        self.deleted.append(obj)
        for k, v in list(self._by_pk.items()):
            if v is obj:
                self._by_pk.pop(k)

    async def flush(self) -> None:
        self.flushes += 1

    async def commit(self) -> None:
        self.commits += 1


def _patch_session_factory(monkeypatch, sessions: list[_FakeSession]):
    """sync_frame opens a fresh SessionLocal() each time it touches the DB —
    return a different fake from the list per call so tests can assert on
    each scope independently."""
    it = iter(sessions)

    def factory():
        return next(it)

    monkeypatch.setattr(sf, "SessionLocal", factory)


def _patch_settings(monkeypatch):
    monkeypatch.setattr(sf.settings, "sync_frame", True)
    monkeypatch.setattr(sf.settings, "frame_workspace_id", "wsP")
    monkeypatch.setattr(sf.settings, "frame_account_id", "aP")
    monkeypatch.setattr(sf.settings, "frame_placeholder_url", "http://x/p.png")
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "Goldbox.no")


def _aval(v):
    async def f(*a, **k):
        return v
    return f


def _patch_notion_task(monkeypatch, *, title="Fasade Nord", project="p1", disc="Eksteriør"):
    monkeypatch.setattr(sf.notion_client, "get_page", _aval({}))
    monkeypatch.setattr(sf.notion_client, "extract_page_title", lambda page: title)
    monkeypatch.setattr(sf.notion_client, "task_project_id", lambda page: project)
    monkeypatch.setattr(sf.notion_client, "task_discipline", lambda page: disc)


def _reset_caches():
    sf._discipline_folder_cache.clear()


# --------------------------------------------------------------------------
# Skip branches
# --------------------------------------------------------------------------


def test_skipped_when_no_title(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, title=None)
    result = asyncio.run(sf.sync_frame_task("t1"))
    assert result.action == "skipped"
    assert result.note == "no title yet"


def test_skipped_when_no_project_relation(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, project=None)
    result = asyncio.run(sf.sync_frame_task("t1"))
    assert result.note == "no project relation"


def test_skipped_when_no_discipline(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, disc=None)
    result = asyncio.run(sf.sync_frame_task("t1"))
    assert result.note == "no Type (discipline) set"


def test_skipped_when_unknown_discipline(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, disc="Lyssetting")
    result = asyncio.run(sf.sync_frame_task("t1"))
    assert "unknown discipline" in (result.note or "")


# --------------------------------------------------------------------------
# Create branch (with cached parent project)
# --------------------------------------------------------------------------


def test_first_sync_creates_task_folder_and_placeholder(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch)

    create_calls: list[tuple] = []
    file_calls: list[tuple] = []

    async def fake_create_folder(parent, name):
        create_calls.append((parent, name))
        return {"id": "newTaskF"}

    async def fake_create_file_from_url(folder_id, name, url):
        file_calls.append((folder_id, name, url))
        return {"id": "newFileF"}

    async def fake_ensure_disc(project_folder_id, discipline_key):
        return "discF"

    async def fake_set_url(*a, **k):
        return None

    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file_from_url)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", fake_ensure_disc)
    # No sibling task folder exists → adoption finds nothing → fresh create.
    monkeypatch.setattr(sf, "_find_child_folder_by_name", _aval(None))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", fake_set_url)
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "x"}))

    sessions = [
        # 1: read project cache (hit) → don't recurse
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        # 2: task work
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "created"
    assert result.frame_folder_id == "newTaskF"
    assert result.frame_placeholder_file_id == "newFileF"
    assert create_calls == [("discF", "Fasade Nord")]
    # The placeholder filename now embeds the project name + studio + task
    # name + V00 version marker. With _FakeProjectRow's default current_name
    # ("Acme") + the task title "Fasade Nord" + the configured studio
    # "Goldbox.no", we expect this exact string.
    assert file_calls == [
        ("newTaskF", "Acme_Goldbox.no_Fasade Nord_V00.png", "http://x/p.png")
    ]
    assert len(sessions[1].added) == 1
    assert sessions[1].commits == 1


# --------------------------------------------------------------------------
# Rename branch
# --------------------------------------------------------------------------


def test_rename_preserves_placeholder_file_id(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, title="New Name")

    rename_calls: list[tuple] = []
    create_calls = 0

    async def fake_rename(folder_id, name):
        rename_calls.append((folder_id, name))
        return {"id": folder_id, "name": name}

    async def fake_create(*a, **k):
        nonlocal create_calls
        create_calls += 1
        return {"id": "nope"}

    monkeypatch.setattr(sf.frame_client, "rename_folder", fake_rename)
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create)
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "taskF"}))
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    task_row = _FakeTaskRow("t1", current_name="Old", current_discipline="Eksteriør")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {("FrameProjectFolder", "p1"): project_row, ("FrameTaskFolder", "t1"): task_row}
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "renamed"
    assert rename_calls == [("taskF", "New Name")]
    assert create_calls == 0
    assert result.frame_placeholder_file_id == "fileF"
    assert task_row.current_name == "New Name"


def test_discipline_change_logs_warning_and_renames_in_place(monkeypatch, caplog):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch, title="Same Name", disc="Interiør")

    monkeypatch.setattr(sf.frame_client, "rename_folder", _aval({"id": "taskF"}))
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "taskF"}))
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF-new"))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    task_row = _FakeTaskRow("t1", current_name="OtherName", current_discipline="Eksteriør")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {("FrameProjectFolder", "p1"): project_row, ("FrameTaskFolder", "t1"): task_row}
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    import logging
    caplog.set_level(logging.WARNING)
    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "renamed"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("discipline changed" in r.getMessage() for r in warnings)


# --------------------------------------------------------------------------
# Self-heal branch
# --------------------------------------------------------------------------


def _make_404():
    import httpx
    response = httpx.Response(
        404, request=httpx.Request("GET", "http://x"), json={"error": "gone"}
    )
    return frame_client.FrameAPIError(response)


def test_self_heal_evicts_stale_task(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch)

    async def fake_get_folder(folder_id):
        raise _make_404()

    create_calls: list[tuple] = []

    async def fake_create_folder(parent, name):
        create_calls.append((parent, name))
        return {"id": "freshTask"}

    async def fake_file(folder_id, name, url):
        return {"id": "freshFile"}

    monkeypatch.setattr(sf.frame_client, "get_folder", fake_get_folder)
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_file)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_folder_by_name", _aval(None))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    task_row = _FakeTaskRow("t1")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {("FrameProjectFolder", "p1"): project_row, ("FrameTaskFolder", "t1"): task_row}
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "created"
    assert result.frame_folder_id == "freshTask"
    assert create_calls == [("discF", "Fasade Nord")]
    assert sessions[1].deleted == [task_row]


# --------------------------------------------------------------------------
# Recursive provisioning
# --------------------------------------------------------------------------


def test_missing_parent_project_is_provisioned_recursively(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch)

    project_calls: list[str] = []

    async def fake_sync_frame_project(page_id):
        project_calls.append(page_id)
        # Stamp the cache so the next session.get returns a row
        sessions[1]._by_pk[("FrameProjectFolder", page_id)] = _FakeProjectRow(page_id)
        from gb_automations.sync.sync_frame import FrameProjectResult
        return FrameProjectResult(project_page_id=page_id, action="created")

    monkeypatch.setattr(sf, "sync_frame_project", fake_sync_frame_project)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_folder_by_name", _aval(None))
    monkeypatch.setattr(sf.frame_client, "create_folder", _aval({"id": "T"}))
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", _aval({"id": "F"}))
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "T"}))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    sessions = [
        _FakeSession({}),  # 1: lookup project → miss
        _FakeSession({}),  # 2: lookup project again after recursion → hit (mutated above)
        _FakeSession({}),  # 3: task work
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert project_calls == ["p1"]
    assert result.action == "created"


# --------------------------------------------------------------------------
# Adoption branch — sibling task folder with the same name already exists
# --------------------------------------------------------------------------


def test_adopts_existing_task_folder_with_existing_placeholder(monkeypatch):
    """A pre-existing task folder under the discipline + an existing
    placeholder.png inside it must be adopted: neither create_folder nor
    create_file_from_url runs, and the cache row picks up both existing ids."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch)

    create_folder_calls = 0
    create_file_calls = 0

    async def fake_create_folder(*a, **k):
        nonlocal create_folder_calls
        create_folder_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_create_file(*a, **k):
        nonlocal create_file_calls
        create_file_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_find_folder(parent, name):
        assert (parent, name) == ("discF", "Fasade Nord")
        return {
            "id": "preExistingTask",
            "name": name,
            "view_url": "https://next.frame.io/project/p/view/preExistingTask",
        }

    async def fake_find_file(parent, name):
        # The placeholder filename is now per-task: <project>_<studio>_<task>_V00.png
        # — built from _FakeProjectRow.current_name ("Acme") + the task title
        # ("Fasade Nord") + the configured studio ("Goldbox.no").
        assert (parent, name) == (
            "preExistingTask",
            "Acme_Goldbox.no_Fasade Nord_V00.png",
        )
        return {"id": "preExistingPlaceholder", "name": name}

    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_folder_by_name", fake_find_folder)
    monkeypatch.setattr(sf, "_find_child_file_by_name", fake_find_file)
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file)
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "x"}))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "adopted"
    assert result.frame_folder_id == "preExistingTask"
    assert result.frame_placeholder_file_id == "preExistingPlaceholder"
    assert create_folder_calls == 0
    assert create_file_calls == 0
    assert (
        result.frame_url
        == "https://next.frame.io/project/p/view/preExistingTask"
    )


def test_adopts_existing_task_folder_uploads_placeholder_when_missing(monkeypatch):
    """Adopted task folder with NO placeholder.png inside: we still adopt the
    folder (no create_folder call) but we DO upload a fresh placeholder so the
    Phase 2 "first upload = V2 of placeholder" workflow stays intact."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_task(monkeypatch)

    create_folder_calls = 0
    upload_calls: list[tuple] = []

    async def fake_create_folder(*a, **k):
        nonlocal create_folder_calls
        create_folder_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_create_file(folder_id, name, url):
        upload_calls.append((folder_id, name, url))
        return {"id": "freshPlaceholder"}

    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(
        sf,
        "_find_child_folder_by_name",
        _aval(
            {
                "id": "preExistingTask",
                "view_url": "https://next.frame.io/project/p/view/preExistingTask",
            }
        ),
    )
    monkeypatch.setattr(sf, "_find_child_file_by_name", _aval(None))
    monkeypatch.setattr(sf.frame_client, "create_folder", fake_create_folder)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file)
    monkeypatch.setattr(sf.frame_client, "get_folder", _aval({"id": "x"}))
    monkeypatch.setattr(sf.notion_client, "set_task_frame_url", _aval(None))

    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_task("t1"))

    assert result.action == "adopted"
    assert result.frame_folder_id == "preExistingTask"
    assert result.frame_placeholder_file_id == "freshPlaceholder"
    assert create_folder_calls == 0
    # Per-task placeholder filename. See test_first_sync_creates_task_folder_and_placeholder
    # for the derivation rule (project + studio + task + V00).
    assert upload_calls == [
        ("preExistingTask", "Acme_Goldbox.no_Fasade Nord_V00.png", "http://x/p.png")
    ]


# --------------------------------------------------------------------------
# Placeholder filename builder (pure function)
# --------------------------------------------------------------------------


def test_placeholder_filename_shape(monkeypatch):
    """Pin the exact filename shape so a future config-name change doesn't
    silently shift it. The order matters: <project>_<studio>_<task>_V00.png."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "Goldbox.no")
    out = sf._placeholder_filename(
        "1230_Metropolis_Orangeriet", "Vinkel 1"
    )
    assert out == "1230_Metropolis_Orangeriet_Goldbox.no_Vinkel 1_V00.png"


def test_placeholder_filename_uses_configured_studio(monkeypatch):
    """The studio slot is configurable — changing FRAME_FILENAME_STUDIO
    flows straight through to new uploads."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "OtherStudio")
    out = sf._placeholder_filename("Proj", "Task")
    assert out == "Proj_OtherStudio_Task_V00.png"


def test_placeholder_filename_falls_back_when_studio_empty(monkeypatch):
    """A blank studio setting would produce a malformed filename
    ('Proj__Task_V00.png'); fall back to 'Goldbox.no' as the default."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "")
    out = sf._placeholder_filename("Proj", "Task")
    assert out == "Proj_Goldbox.no_Task_V00.png"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
