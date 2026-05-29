"""Unit tests for sync_frame.sync_frame_project.

Pins the behaviors the Frame mirror must hold without hitting Postgres,
Frame.io, or Notion:

  1. SYNC_FRAME=false / no workspace → skipped (no API calls).
  2. No Notion title → skipped with note (no API calls).
  3. First sync → create_project runs against the configured workspace, URL
     is patched back onto the Notion page.
  4. Rename branch → rename_project runs; create_project does NOT.
  5. Self-heal → cached project id that 404s is evicted and a fresh
     create_project runs.
  6. Notion writeback failure does not flip the result to "failed" (the
     project exists; the next run retries the patch).
  7. Adoption — a same-name Frame Project pre-existing in the workspace
     is adopted, not duplicated. Two variants: with and without view_url
     on the list_projects response.
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
    frame_project_id: str = "old-project-id"
    frame_folder_id: str = "old-root-folder-id"  # the Project's root_folder_id
    current_name: str = "Old name"
    frame_url: str = "https://next.frame.io/project/old-project-id"


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


def _patch_settings(
    monkeypatch,
    *,
    sync_frame=True,
    workspace_id="wsP",
    account_id="aP",
    placeholder="http://x/p.png",
):
    monkeypatch.setattr(sf.settings, "sync_frame", sync_frame)
    monkeypatch.setattr(sf.settings, "frame_workspace_id", workspace_id)
    monkeypatch.setattr(sf.settings, "frame_account_id", account_id)
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
    sf._discipline_folder_cache.clear()


# --------------------------------------------------------------------------
# Skipped branches
# --------------------------------------------------------------------------


def test_skipped_when_sync_frame_off(monkeypatch):
    _patch_settings(monkeypatch, sync_frame=False)
    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "skipped"
    assert result.note == "SYNC_FRAME=false"


def test_skipped_when_workspace_unset(monkeypatch):
    _patch_settings(monkeypatch, workspace_id="")
    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "skipped"
    assert result.note == "FRAME_WORKSPACE_ID not set"


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


def test_first_sync_creates_project_and_writes_url(monkeypatch):
    """No cache row + no workspace sibling → create_project under workspace +
    cache row inserted with both project_id and root_folder_id + URL patched."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    create_calls: list[tuple] = []

    async def fake_create_project(workspace_id, name):
        create_calls.append((workspace_id, name))
        return {
            "id": "newProj",
            "name": name,
            "root_folder_id": "newRoot",
            "view_url": "https://next.frame.io/project/newProj",
        }

    async def fake_get_project(_):
        raise AssertionError("self-heal path must not run on fresh sync")

    set_url_calls: list[tuple] = []

    async def fake_set_url(page_id, url):
        set_url_calls.append((page_id, url))

    # No sibling project exists → adoption finds nothing → fresh create.
    monkeypatch.setattr(sf, "_find_workspace_project_by_name", _aval(None))
    monkeypatch.setattr(sf.frame_client, "create_project", fake_create_project)
    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", fake_set_url)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "created"
    assert result.frame_project_id == "newProj"
    assert result.frame_folder_id == "newRoot"
    assert result.frame_url == "https://next.frame.io/project/newProj"
    assert create_calls == [("wsP", "Acme")]
    assert set_url_calls == [("p1", "https://next.frame.io/project/newProj")]
    assert len(session.added) == 1
    assert session.commits == 1


def test_create_raises_when_response_missing_root_folder(monkeypatch):
    """Defensive: if Frame ever stops returning root_folder_id on create, we
    raise loudly so downstream task-folder creation doesn't silently break."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    monkeypatch.setattr(sf, "_find_workspace_project_by_name", _aval(None))
    monkeypatch.setattr(
        sf.frame_client,
        "create_project",
        _aval({"id": "newProj", "name": "Acme"}),  # no root_folder_id
    )
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    # The outer try/except in sync_frame_project catches and marks failed.
    assert result.action == "failed"


# --------------------------------------------------------------------------
# Rename branch
# --------------------------------------------------------------------------


def test_rename_calls_rename_project_only(monkeypatch):
    """Project name changed in Notion → PATCH the Frame Project. The Project
    id (and its root_folder_id) stay the same, so cached child folders remain
    reachable."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="New Name")

    create_calls = 0
    rename_calls: list[tuple] = []

    async def fake_create_project(*a, **k):
        nonlocal create_calls
        create_calls += 1
        return {"id": "shouldnt-be-called"}

    async def fake_rename_project(project_id, new_name):
        rename_calls.append((project_id, new_name))
        return {
            "id": project_id,
            "name": new_name,
            "view_url": f"https://next.frame.io/project/{project_id}",
        }

    async def fake_get_project(project_id):
        return {"id": project_id}  # live → not stale

    monkeypatch.setattr(sf.frame_client, "create_project", fake_create_project)
    monkeypatch.setattr(sf.frame_client, "rename_project", fake_rename_project)
    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    row = _FakeProjectRow(
        notion_page_id="p1",
        frame_project_id="old-proj",
        frame_folder_id="old-root",
        current_name="Old Name",
    )
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "renamed"
    assert rename_calls == [("old-proj", "New Name")]
    assert create_calls == 0
    assert row.current_name == "New Name"  # mutation persisted via merge
    assert result.frame_project_id == "old-proj"
    # frame_folder_id (the project's root_folder_id) is still the same — the
    # rename of a Frame Project preserves its root folder id.
    assert result.frame_folder_id == "old-root"


def test_unchanged_when_name_matches(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="SameName")

    async def fake_get_project(project_id):
        return {"id": project_id}

    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(
        sf.frame_client, "create_project", _aval({"id": "nope"})
    )
    monkeypatch.setattr(
        sf.frame_client, "rename_project", _aval({"id": "nope"})
    )
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    row = _FakeProjectRow(notion_page_id="p1", current_name="SameName")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "unchanged"
    assert result.frame_project_id == row.frame_project_id
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


def test_self_heal_on_stale_project_id(monkeypatch):
    """Cached Frame Project id 404s on get_project → cache row evicted →
    fresh create_project runs to mint a new Project."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    async def fake_get_project(project_id):
        raise _make_404()

    create_calls: list[tuple] = []

    async def fake_create_project(workspace_id, name):
        create_calls.append((workspace_id, name))
        return {
            "id": "fresh",
            "root_folder_id": "freshRoot",
            "view_url": "https://next.frame.io/project/fresh",
        }

    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(sf, "_find_workspace_project_by_name", _aval(None))
    monkeypatch.setattr(sf.frame_client, "create_project", fake_create_project)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    row = _FakeProjectRow(notion_page_id="p1", current_name="Acme")
    session = _FakeSession(get_returns=row)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "created"
    assert result.frame_project_id == "fresh"
    assert result.frame_folder_id == "freshRoot"
    assert create_calls == [("wsP", "Acme")]
    assert session.deleted == [row]


# --------------------------------------------------------------------------
# Notion writeback resilience
# --------------------------------------------------------------------------


def test_notion_writeback_failure_does_not_fail_the_task(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    monkeypatch.setattr(sf, "_find_workspace_project_by_name", _aval(None))
    monkeypatch.setattr(
        sf.frame_client,
        "create_project",
        _aval(
            {
                "id": "P",
                "root_folder_id": "R",
                "view_url": "https://next.frame.io/project/P",
            }
        ),
    )
    monkeypatch.setattr(sf.frame_client, "get_project", _aval({"id": "P"}))

    async def boom(*a, **k):
        raise RuntimeError("Notion is down")

    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", boom)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    # The project was created; URL retry will happen on the next sync.
    assert result.action == "created"
    assert result.frame_project_id == "P"
    assert result.frame_folder_id == "R"


# --------------------------------------------------------------------------
# Failure path
# --------------------------------------------------------------------------


def test_frame_api_failure_marks_action_failed(monkeypatch):
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    async def boom(*a, **k):
        raise RuntimeError("frame down")

    monkeypatch.setattr(sf, "_find_workspace_project_by_name", boom)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))
    assert result.action == "failed"


# --------------------------------------------------------------------------
# Adoption branch — Frame Project with the same name already exists
# --------------------------------------------------------------------------


def test_adopts_existing_workspace_project(monkeypatch):
    """A Frame Project pre-created in the workspace must be adopted, not
    duplicated. create_project must not run; the cache row picks up the
    existing project's id + root_folder_id, and the URL written back is
    the existing project's view_url."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    create_calls = 0

    async def fake_find(workspace_id, name):
        # Match by workspace_id/name — the project-level adoption path.
        assert (workspace_id, name) == ("wsP", "Acme")
        return {
            "id": "preExisting",
            "name": "Acme",
            "root_folder_id": "preRoot",
            "view_url": "https://next.frame.io/project/preExisting",
        }

    async def fake_create_project(*a, **k):
        nonlocal create_calls
        create_calls += 1
        return {"id": "should-not-be-called"}

    async def fake_get_project(_):
        raise AssertionError("get_project must not run when view_url + root present")

    set_url_calls: list[tuple] = []

    async def fake_set_url(page_id, url):
        set_url_calls.append((page_id, url))

    children_listed: list[str] = []

    async def fake_list_children(folder_id):
        # The adopt branch audits pre-existing root contents for the log line.
        children_listed.append(folder_id)
        return [{"id": "f1", "name": "Interiør", "type": "folder"}]

    monkeypatch.setattr(sf, "_find_workspace_project_by_name", fake_find)
    monkeypatch.setattr(sf.frame_client, "create_project", fake_create_project)
    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(sf.frame_client, "list_folder_children", fake_list_children)
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", fake_set_url)

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "adopted"
    # The adoption audit log must inspect the adopted project's root folder.
    assert children_listed == ["preRoot"]
    assert result.frame_project_id == "preExisting"
    assert result.frame_folder_id == "preRoot"
    assert result.frame_url == "https://next.frame.io/project/preExisting"
    assert create_calls == 0
    assert len(session.added) == 1
    assert set_url_calls == [("p1", "https://next.frame.io/project/preExisting")]


def test_adopts_existing_then_fetches_when_view_url_missing(monkeypatch):
    """list_projects doesn't always include view_url or root_folder_id on every
    entry. When either is missing on the adopted child, we MUST call
    get_project so the stored URL is real and we have a root_folder_id."""
    _reset_caches()
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, title="Acme")

    async def fake_find(workspace_id, name):
        # No view_url + no root_folder_id → triggers the get_project fetch.
        return {"id": "preExisting", "name": "Acme"}

    get_project_calls: list[str] = []

    async def fake_get_project(project_id):
        get_project_calls.append(project_id)
        return {
            "id": project_id,
            "root_folder_id": "preRoot",
            "view_url": "https://next.frame.io/project/preExisting",
        }

    async def fake_create_project(*a, **k):
        raise AssertionError("create_project must not run on adoption")

    monkeypatch.setattr(sf, "_find_workspace_project_by_name", fake_find)
    monkeypatch.setattr(sf.frame_client, "get_project", fake_get_project)
    monkeypatch.setattr(sf.frame_client, "create_project", fake_create_project)
    monkeypatch.setattr(sf.frame_client, "list_folder_children", _aval([]))
    monkeypatch.setattr(sf.notion_client, "set_project_frame_url", _aval(None))

    session = _FakeSession(get_returns=None)
    _patch_session(monkeypatch, session)

    result = asyncio.run(sf.sync_frame_project("p1"))

    assert result.action == "adopted"
    assert result.frame_project_id == "preExisting"
    assert result.frame_folder_id == "preRoot"
    assert get_project_calls == ["preExisting"]
    assert result.frame_url == "https://next.frame.io/project/preExisting"


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload
        self.is_success = True
        self.status_code = 200

    def json(self):
        return self._payload


class _PagingClient:
    """Fake httpx client returning a scripted sequence of pages keyed by the
    path requested. Records every path so we can assert the cursor was
    followed."""

    def __init__(self, pages_by_path):
        self._pages = pages_by_path
        self.requested: list[str] = []

    async def get(self, path):
        self.requested.append(path)
        return _FakeResp(self._pages[path])


def test_get_all_pages_follows_links_next():
    """_get_all_pages must concatenate every page so adoption sees entries on
    later pages — otherwise a same-name folder/project past page 1 is missed
    and we'd create a duplicate."""
    base = "/accounts/a/workspaces/w/projects"
    client = _PagingClient(
        {
            base: {
                "data": [{"id": "1", "name": "A"}],
                "links": {"next": frame_client.FRAME_API_BASE + base + "?cursor=c2"},
            },
            base + "?cursor=c2": {
                "data": [{"id": "2", "name": "B"}],
                "links": {},
            },
        }
    )
    items = asyncio.run(
        frame_client._get_all_pages(client, base, op_name="list_projects")
    )
    assert [i["id"] for i in items] == ["1", "2"]
    assert client.requested == [base, base + "?cursor=c2"]


def test_get_all_pages_single_page_stops():
    """No next cursor → exactly one request, no duplicate fetches."""
    base = "/accounts/a/folders/f/children"
    client = _PagingClient({base: {"data": [{"id": "1"}], "links": {}}})
    items = asyncio.run(
        frame_client._get_all_pages(client, base, op_name="list_folder_children")
    )
    assert [i["id"] for i in items] == ["1"]
    assert client.requested == [base]


def test_get_all_pages_pagination_cursor_token():
    """The `pagination.next_cursor` shape is followed too (not just links.next)."""
    base = "/accounts/a/folders/f/children"
    client = _PagingClient(
        {
            base: {
                "data": [{"id": "1"}],
                "pagination": {"next_cursor": "tok"},
            },
            base + "?cursor=tok": {"data": [{"id": "2"}], "pagination": {}},
        }
    )
    items = asyncio.run(
        frame_client._get_all_pages(client, base, op_name="list_folder_children")
    )
    assert [i["id"] for i in items] == ["1", "2"]


def test_get_all_pages_breaks_on_repeated_path():
    """A cursor that points back at a path already seen must not loop forever."""
    base = "/accounts/a/folders/f/children"
    client = _PagingClient(
        {
            base: {
                "data": [{"id": "1"}],
                "links": {"next": frame_client.FRAME_API_BASE + base},
            }
        }
    )
    items = asyncio.run(
        frame_client._get_all_pages(client, base, op_name="list_folder_children")
    )
    assert [i["id"] for i in items] == ["1"]
    assert client.requested == [base]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
