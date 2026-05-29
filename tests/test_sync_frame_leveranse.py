"""Unit tests for sync_frame.sync_frame_leveranse (flattened layout).

After the May 2026 restructure the Frame mirror is leveranse-based and
flattened: a deliverable's placeholder file sits DIRECTLY under the project's
discipline subfolder — there is no per-leveranse wrapping folder. The
per-leveranse anchor is `frame_placeholder_file_id`; `frame_folder_id` stores
the SHARED discipline folder id. These tests pin that behavior without
touching the DB / Frame.io / Notion:

  1. Skip branches (no title, no project relation, no discipline,
     unrecognized discipline).
  2. First sync uploads the placeholder file under the discipline folder and
     writes the URL back.
  3. Rename renames the placeholder FILE (id preserved) — the filename is the
     visible label under the flattened layout.
  4. Discipline change is logged as a warning and renamed in place
     (re-parenting deferred).
  5. Self-heal: a stale cached placeholder file (404 on get_file) → evict →
     re-create.
  6. Recursive provisioning: a missing parent project is created on demand so
     the queue can drain leveranse tasks before project tasks.
  7. Adoption: a pre-existing same-name placeholder file is matched instead of
     re-uploaded (incl. the get_file fallback when it carries no view_url).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import pytest

import gb_automations.sync.sync_frame as sf
from gb_automations.clients import frame as frame_client


@dataclass
class _FakeProjectRow:
    # frame_folder_id here is the Project's root_folder_id (the parent passed
    # to _ensure_discipline_folder).
    notion_page_id: str
    frame_project_id: str = "projID"
    frame_folder_id: str = "projF"
    current_name: str = "Acme"
    frame_url: str = "u"


@dataclass
class _FakeLeveranseRow:
    # Models FrameLeveranseFolder: frame_folder_id is the SHARED discipline
    # folder; frame_placeholder_file_id is the per-leveranse anchor.
    notion_page_id: str
    project_page_id: str = "p1"
    frame_folder_id: str = "discF"
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
    return a different fake from the list per call so tests can assert on each
    scope independently."""
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


def _patch_notion_leveranse(
    monkeypatch, *, title="Fasade Nord", project="p1", disc="Eksteriør"
):
    monkeypatch.setattr(sf.notion_client, "get_page", _aval({}))
    monkeypatch.setattr(sf.notion_client, "extract_page_title", lambda page: title)
    monkeypatch.setattr(sf.notion_client, "task_project_id", lambda page: project)
    monkeypatch.setattr(sf.notion_client, "task_discipline", lambda page: disc)
    # Tail-end Notion writes — patched so tests never hit the network. The
    # status branch only fires when get_deliverable_status returns None
    # (= fresh provision); both paths are best-effort in the engine.
    monkeypatch.setattr(sf.notion_client, "set_deliverable_frame_url", _aval(None))
    monkeypatch.setattr(sf.notion_client, "get_deliverable_status", _aval(None))
    monkeypatch.setattr(sf.notion_client, "set_deliverable_status", _aval(None))


def _reset_caches():
    sf._discipline_folder_cache.clear()


# --------------------------------------------------------------------------
# Skip branches
# --------------------------------------------------------------------------


def test_skipped_when_no_title(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch, title=None)
    result = asyncio.run(sf.sync_frame_leveranse("t1"))
    assert result.note == "no title yet"


def test_skipped_when_no_project_relation(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch, project=None)
    result = asyncio.run(sf.sync_frame_leveranse("t1"))
    assert result.note == "no project relation"


def test_skipped_when_no_discipline(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch, disc=None)
    result = asyncio.run(sf.sync_frame_leveranse("t1"))
    assert result.note == "no Type (discipline) set"


def test_skipped_when_unknown_discipline(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch, disc="Lyssetting")
    result = asyncio.run(sf.sync_frame_leveranse("t1"))
    assert "unknown discipline" in (result.note or "")


# --------------------------------------------------------------------------
# Create branch (flattened: upload placeholder file under discipline folder)
# --------------------------------------------------------------------------


def test_first_sync_creates_placeholder(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch)

    file_calls: list[tuple] = []

    async def fake_create_file_from_url(folder_id, name, url):
        file_calls.append((folder_id, name, url))
        return {"id": "newFileF", "view_url": "http://frame/view/newFileF"}

    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    # No existing placeholder file → fresh upload.
    monkeypatch.setattr(sf, "_find_child_file_by_name", _aval(None))
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file_from_url)

    sessions = [
        # 1: read project cache (hit) → don't recurse
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        # 2: leveranse work
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "created"
    assert result.frame_placeholder_file_id == "newFileF"
    # frame_folder_id is the SHARED discipline folder, not a per-leveranse one.
    # The placeholder filename is <leveranse>_<studio>_V00: "Fasade Nord" +
    # studio "Goldbox.no" (no project prefix).
    # Placeholder source is the per-deliverable dynamic render endpoint, with
    # the origin derived from frame_placeholder_url (http://x/p.png → http://x).
    assert file_calls == [
        ("discF", "Fasade Nord_Goldbox.no_V00.png", "http://x/assets/placeholder/t1.png")
    ]
    assert len(sessions[1].added) == 1
    added = sessions[1].added[0]
    assert added.frame_folder_id == "discF"
    assert added.frame_placeholder_file_id == "newFileF"
    assert sessions[1].commits == 1


# --------------------------------------------------------------------------
# Rename branch — renames the placeholder FILE, id preserved
# --------------------------------------------------------------------------


def test_rename_renames_placeholder_file(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch, title="New Name")

    rename_calls: list[tuple] = []
    create_calls = 0

    async def fake_rename_file(file_id, name):
        rename_calls.append((file_id, name))
        return {"id": file_id, "name": name}

    async def fake_create(*a, **k):
        nonlocal create_calls
        create_calls += 1
        return {"id": "nope"}

    monkeypatch.setattr(sf.frame_client, "rename_file", fake_rename_file)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    # get_file must succeed so the stale-check doesn't evict the row.
    monkeypatch.setattr(sf.frame_client, "get_file", _aval({"id": "fileF"}))

    lev_row = _FakeLeveranseRow("t1", current_name="Old", current_discipline="Eksteriør")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {
                ("FrameProjectFolder", "p1"): project_row,
                ("FrameLeveranseFolder", "t1"): lev_row,
            }
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "renamed"
    assert rename_calls == [("fileF", "New Name_Goldbox.no_V00.png")]
    assert create_calls == 0
    assert result.frame_placeholder_file_id == "fileF"
    assert lev_row.current_name == "New Name"


def test_discipline_change_logs_warning_and_renames_in_place(monkeypatch, caplog):
    _reset_caches()
    _patch_settings(monkeypatch)
    # Name differs from cached "Old" so the rename branch is entered; discipline
    # also differs ("Eksteriør" → "Interiør") so the warning fires.
    _patch_notion_leveranse(monkeypatch, title="Same Name", disc="Interiør")

    monkeypatch.setattr(sf.frame_client, "rename_file", _aval({"id": "fileF"}))
    monkeypatch.setattr(sf.frame_client, "get_file", _aval({"id": "fileF"}))
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF-new"))

    lev_row = _FakeLeveranseRow("t1", current_name="OtherName", current_discipline="Eksteriør")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {
                ("FrameProjectFolder", "p1"): project_row,
                ("FrameLeveranseFolder", "t1"): lev_row,
            }
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    caplog.set_level(logging.WARNING)
    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "renamed"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("discipline changed" in r.getMessage() for r in warnings)


# --------------------------------------------------------------------------
# Self-heal branch — stale placeholder file (404 on get_file)
# --------------------------------------------------------------------------


def _make_404():
    import httpx
    response = httpx.Response(
        404, request=httpx.Request("GET", "http://x"), json={"error": "gone"}
    )
    return frame_client.FrameAPIError(response)


def test_self_heal_evicts_stale_leveranse(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch)

    async def fake_get_file(file_id):
        raise _make_404()

    file_calls: list[tuple] = []

    async def fake_create_file(folder_id, name, url):
        file_calls.append((folder_id, name, url))
        return {"id": "freshFile", "view_url": "http://frame/view/freshFile"}

    monkeypatch.setattr(sf.frame_client, "get_file", fake_get_file)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_file_by_name", _aval(None))

    lev_row = _FakeLeveranseRow("t1")
    project_row = _FakeProjectRow("p1")
    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): project_row}),
        _FakeSession(
            {
                ("FrameProjectFolder", "p1"): project_row,
                ("FrameLeveranseFolder", "t1"): lev_row,
            }
        ),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "created"
    assert result.frame_placeholder_file_id == "freshFile"
    assert file_calls == [
        ("discF", "Fasade Nord_Goldbox.no_V00.png", "http://x/assets/placeholder/t1.png")
    ]
    assert sessions[1].deleted == [lev_row]


# --------------------------------------------------------------------------
# Recursive provisioning
# --------------------------------------------------------------------------


def test_missing_parent_project_is_provisioned_recursively(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch)

    project_calls: list[str] = []

    async def fake_sync_frame_project(page_id):
        project_calls.append(page_id)
        # Stamp the cache so the next session.get returns a row.
        sessions[1]._by_pk[("FrameProjectFolder", page_id)] = _FakeProjectRow(page_id)
        from gb_automations.sync.sync_frame import FrameProjectResult
        return FrameProjectResult(project_page_id=page_id, action="created")

    monkeypatch.setattr(sf, "sync_frame_project", fake_sync_frame_project)
    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_file_by_name", _aval(None))
    monkeypatch.setattr(
        sf.frame_client,
        "create_file_from_url",
        _aval({"id": "F", "view_url": "http://frame/view/F"}),
    )

    sessions = [
        _FakeSession({}),  # 1: lookup project → miss
        _FakeSession({}),  # 2: lookup project again after recursion → hit (mutated above)
        _FakeSession({}),  # 3: leveranse work
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert project_calls == ["p1"]
    assert result.action == "created"


# --------------------------------------------------------------------------
# Adoption branch — a same-name placeholder file already exists
# --------------------------------------------------------------------------


def test_adopts_existing_placeholder_with_view_url(monkeypatch):
    """A pre-existing placeholder file under the discipline folder must be
    adopted: create_file_from_url does NOT run, and the cache row + result
    pick up the existing file id and its view_url."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch)

    create_file_calls = 0

    async def fake_create_file(*a, **k):
        nonlocal create_file_calls
        create_file_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_find_file(parent, name):
        assert (parent, name) == ("discF", "Fasade Nord_Goldbox.no_V00.png")
        return {
            "id": "preExistingFile",
            "name": name,
            "view_url": "https://next.frame.io/project/p/view/preExistingFile",
        }

    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    monkeypatch.setattr(sf, "_find_child_file_by_name", fake_find_file)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file)

    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "adopted"
    assert result.frame_placeholder_file_id == "preExistingFile"
    assert create_file_calls == 0
    assert result.frame_url == "https://next.frame.io/project/p/view/preExistingFile"


def test_adopts_existing_placeholder_fetches_url_when_missing(monkeypatch):
    """An adopted placeholder file that comes back WITHOUT a view_url triggers
    a get_file re-fetch to resolve the URL (the engine's fallback at the adopt
    branch). Still adopted — no upload."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion_leveranse(monkeypatch)

    create_file_calls = 0
    get_file_calls: list[str] = []

    async def fake_create_file(*a, **k):
        nonlocal create_file_calls
        create_file_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_get_file(file_id):
        get_file_calls.append(file_id)
        return {
            "id": file_id,
            "view_url": "https://next.frame.io/project/p/view/preExistingFile",
        }

    monkeypatch.setattr(sf, "_ensure_discipline_folder", _aval("discF"))
    # Existing file with NO view_url → engine must re-fetch via get_file.
    monkeypatch.setattr(
        sf, "_find_child_file_by_name", _aval({"id": "preExistingFile"})
    )
    monkeypatch.setattr(sf.frame_client, "get_file", fake_get_file)
    monkeypatch.setattr(sf.frame_client, "create_file_from_url", fake_create_file)

    sessions = [
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
        _FakeSession({("FrameProjectFolder", "p1"): _FakeProjectRow("p1")}),
    ]
    _patch_session_factory(monkeypatch, sessions)

    result = asyncio.run(sf.sync_frame_leveranse("t1"))

    assert result.action == "adopted"
    assert result.frame_placeholder_file_id == "preExistingFile"
    assert create_file_calls == 0
    assert get_file_calls == ["preExistingFile"]
    assert result.frame_url == "https://next.frame.io/project/p/view/preExistingFile"


# --------------------------------------------------------------------------
# Placeholder filename builder (pure function) — unchanged behavior
# --------------------------------------------------------------------------


def test_placeholder_filename_shape(monkeypatch):
    """Pin the exact filename shape so a future config-name change doesn't
    silently shift it. The order matters: <task>_<studio>_V00.png (no project
    prefix)."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "Goldbox.no")
    out = sf._placeholder_filename("1230_Metropolis_Orangeriet", "Vinkel 1")
    assert out == "Vinkel 1_Goldbox.no_V00.png"


def test_placeholder_filename_uses_configured_studio(monkeypatch):
    """The studio slot is configurable — changing FRAME_FILENAME_STUDIO flows
    straight through to new uploads."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "OtherStudio")
    out = sf._placeholder_filename("Proj", "Task")
    assert out == "Task_OtherStudio_V00.png"


def test_placeholder_filename_falls_back_when_studio_empty(monkeypatch):
    """A blank studio setting would produce a malformed filename
    ('Task__V00.png'); fall back to 'Goldbox.no' as the default."""
    monkeypatch.setattr(sf.settings, "frame_filename_studio", "")
    out = sf._placeholder_filename("Proj", "Task")
    assert out == "Task_Goldbox.no_V00.png"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
