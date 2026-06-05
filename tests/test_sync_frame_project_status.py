"""Unit tests for sync_frame_project_status.

Pins the behavior the Notion-only project lifecycle mirror must hold without
hitting Postgres, Frame.io, or Notion:

  1. SYNC_FRAME=false → skipped (no API calls).
  2. No FrameProjectFolder cache row → skipped (project not yet provisioned
     in Frame; nothing to flip).
  3. Notion status in PROJECT_STATUS_INACTIVE_TRIGGERS → Frame PATCHed
     to status="inactive" (when current ≠ desired).
  4. Notion status NOT in triggers → Frame PATCHed to status="active"
     (when current ≠ desired).
  5. Empty / unset Notion status → Frame PATCHed to status="active"
     (reopening a project by clearing Status re-activates the Frame entity).
  6. Frame already at the desired status → loop guard: no PATCH, action
     "unchanged".
  7. Frame project 404 → skipped with note (deleted; next provisioning
     run will re-create).
  8. Notion get_page failure → action "failed" (queue will retry).
  9. Frame set_project_status failure → action "failed" (queue will retry).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

import gb_automations.sync.sync_frame_project_status as sps
from gb_automations.clients import frame as frame_client
from gb_automations.config import (
    PROJECT_STATUS_FERDIG,
    PROJECT_STATUS_I_PRODUKSJON,
    PROJECT_STATUS_TAPT,
    PROJECT_STATUS_TILBUDSFASE,
)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class _FakeProjectRow:
    notion_page_id: str
    frame_project_id: str = "frame-proj-id"
    frame_folder_id: str = "frame-root-folder-id"
    current_name: str = "Acme"
    frame_url: str = "https://next.frame.io/project/frame-proj-id"


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    """Minimal async-session stub covering `await session.execute(stmt)` →
    `.scalar_one_or_none()`. The engine doesn't add/merge/delete on this DB
    read path — it only looks up the FrameProjectFolder row."""

    def __init__(self, row_returns=None):
        self._row = row_returns
        self.execute_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _stmt):
        self.execute_calls += 1
        return _FakeScalarResult(self._row)


def _patch_session(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr(sps, "SessionLocal", lambda: session)


def _patch_settings(monkeypatch, *, sync_frame=True):
    monkeypatch.setattr(sps.settings, "sync_frame", sync_frame, raising=False)


def _patch_notion(monkeypatch, *, status: str | None):
    """Stub Notion to return a page whose extract_project_status returns `status`."""

    async def fake_get_page(_page_id: str) -> dict:
        return {"id": _page_id, "properties": {}}

    monkeypatch.setattr(sps.notion_client, "get_page", fake_get_page)
    monkeypatch.setattr(
        sps.notion_client, "extract_project_status", lambda _page: status
    )


def _patch_notion_get_page_raises(monkeypatch, err: Exception):
    async def boom(_page_id: str) -> dict:
        raise err

    monkeypatch.setattr(sps.notion_client, "get_page", boom)


def _patch_frame(
    monkeypatch,
    *,
    get_status: str | None = "active",
    get_raises: Exception | None = None,
    set_raises: Exception | None = None,
):
    """Stub frame.get_project and frame.set_project_status. Captures PATCH calls
    so tests can assert on the desired status that got written."""

    patch_calls: list[tuple[str, str]] = []

    async def fake_get(_project_id: str) -> dict:
        if get_raises is not None:
            raise get_raises
        return {"id": _project_id, "status": get_status}

    async def fake_set(project_id: str, status: str) -> dict:
        if set_raises is not None:
            raise set_raises
        patch_calls.append((project_id, status))
        return {"id": project_id, "status": status}

    monkeypatch.setattr(sps.frame_client, "get_project", fake_get)
    monkeypatch.setattr(sps.frame_client, "set_project_status", fake_set)
    return patch_calls


def _frame_404() -> frame_client.FrameAPIError:
    err = frame_client.FrameAPIError.__new__(frame_client.FrameAPIError)
    RuntimeError.__init__(err, "Frame.io 404 not found")
    err.status_code = 404
    return err


# --------------------------------------------------------------------------
# Skipped branches
# --------------------------------------------------------------------------


def test_skipped_when_sync_frame_off(monkeypatch):
    _patch_settings(monkeypatch, sync_frame=False)
    # Nothing else should be reached.
    monkeypatch.setattr(
        sps.notion_client, "get_page",
        lambda *_a, **_kw: (_ for _ in ()).throw(
            AssertionError("get_page must not be reached with sync_frame=False")
        ),
    )

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "skipped"
    assert result.note == "SYNC_FRAME=false"


def test_skipped_when_no_cache_row(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=PROJECT_STATUS_FERDIG)
    _patch_session(monkeypatch, _FakeSession(row_returns=None))
    # If Frame were reached this would error.
    patches = _patch_frame(
        monkeypatch,
        get_raises=AssertionError("frame.get_project must not be reached"),
    )

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "skipped"
    assert result.note == "no FrameProjectFolder cache row"
    assert result.notion_status == PROJECT_STATUS_FERDIG
    assert result.desired_frame_status == "inactive"
    # No PATCH was attempted.
    assert patches == []


# --------------------------------------------------------------------------
# Mapping: Notion status → Frame status
# --------------------------------------------------------------------------


@pytest.mark.parametrize("inactive_status", [PROJECT_STATUS_FERDIG, PROJECT_STATUS_TAPT])
def test_terminal_statuses_set_frame_inactive(monkeypatch, inactive_status):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=inactive_status)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    patches = _patch_frame(monkeypatch, get_status="active")

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "written"
    assert result.desired_frame_status == "inactive"
    assert patches == [("frame-proj-id", "inactive")]


@pytest.mark.parametrize(
    "active_status", [PROJECT_STATUS_TILBUDSFASE, PROJECT_STATUS_I_PRODUKSJON]
)
def test_non_terminal_status_sets_frame_active(monkeypatch, active_status):
    # Project was previously inactivated; status moves back to a non-terminal
    # value → Frame project must be re-activated.
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=active_status)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    patches = _patch_frame(monkeypatch, get_status="inactive")

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "written"
    assert result.desired_frame_status == "active"
    assert patches == [("frame-proj-id", "active")]


def test_empty_status_sets_frame_active(monkeypatch):
    # Clearing Status (or never setting it) should put Frame at "active" —
    # the default state. A previously-inactivated project gets re-activated
    # when the team clears the Status to reopen.
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=None)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    patches = _patch_frame(monkeypatch, get_status="inactive")

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "written"
    assert result.desired_frame_status == "active"
    assert patches == [("frame-proj-id", "active")]


# --------------------------------------------------------------------------
# Loop guard / idempotency
# --------------------------------------------------------------------------


def test_loop_guard_skips_when_already_at_desired(monkeypatch):
    # Frame already at "inactive" + Notion still at Ferdig → no PATCH.
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=PROJECT_STATUS_FERDIG)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    patches = _patch_frame(monkeypatch, get_status="inactive")

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "unchanged"
    assert result.current_frame_status == "inactive"
    assert result.desired_frame_status == "inactive"
    assert patches == []


def test_loop_guard_skips_when_already_active(monkeypatch):
    # A project at I produksjon while Frame is already "active" is the
    # common case — no churn. Same loop guard.
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=PROJECT_STATUS_I_PRODUKSJON)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    patches = _patch_frame(monkeypatch, get_status="active")

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "unchanged"
    assert patches == []


# --------------------------------------------------------------------------
# Error / 404 handling
# --------------------------------------------------------------------------


def test_frame_get_404_is_clean_skip(monkeypatch):
    # The cached frame_project_id is stale (project deleted in Frame). We
    # surface this as a skip rather than a failure — the queue should not
    # keep retrying a project that no longer exists. Next provisioning run
    # will re-create.
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=PROJECT_STATUS_FERDIG)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    _patch_frame(monkeypatch, get_raises=_frame_404())

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "skipped"
    assert result.note == "Frame project 404 (deleted)"


def test_notion_get_page_failure_marks_failed(monkeypatch):
    # Transient Notion error → action "failed" so the queue retries with
    # backoff (not a silent skip — we don't actually know whether the
    # project is still at Ferdig or not).
    _patch_settings(monkeypatch)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    _patch_notion_get_page_raises(monkeypatch, RuntimeError("notion 503"))

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "failed"
    assert "notion 503" in (result.note or "").lower()


def test_frame_set_failure_marks_failed(monkeypatch):
    _patch_settings(monkeypatch)
    _patch_notion(monkeypatch, status=PROJECT_STATUS_FERDIG)
    _patch_session(monkeypatch, _FakeSession(row_returns=_FakeProjectRow("p1")))
    _patch_frame(
        monkeypatch,
        get_status="active",
        set_raises=RuntimeError("frame 500"),
    )

    result = asyncio.run(sps.sync_frame_project_status("p1"))
    assert result.action == "failed"
    assert "frame 500" in (result.note or "").lower()
