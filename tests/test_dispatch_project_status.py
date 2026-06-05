"""Unit tests for sync.dispatch_project_status.dispatch_project_status.

This is the worker-side handler for a `project_status_dispatch` task. The
webhook acks Notion immediately and enqueues one of these; the worker drains
it and fans out to every per-engine task type. Tests pin:

  1. Placeholder-title gate — `000_Kunde_Prosjekt TEMPLATE` skips every engine.
  2. Status mapping (Tilbudsfase / Tilbud godkjent / I produksjon → which
     engines get queued; Ferdig/Tapt → none in provisioning fan-out but
     frame_status still fires).
  3. Frame project active/inactive lane — fires on EVERY status change,
     including empty / unmapped.
  4. Per-engine env-flag skips surface as `skipped: <reason>` (not silent).
  5. Multi-select / select / status property shapes all read correctly.
  6. Idempotent re-enqueue surfaces as `already_queued`.
  7. Frame deliverable fan-out is invoked when frame is in the engine set.
  8. Notion get_page failure → result.action="failed" (queue retries).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import gb_automations.sync.dispatch_project_status as disp
from gb_automations.config import (
    PROJECT_STATUS_FERDIG,
    PROJECT_STATUS_I_PRODUKSJON,
    PROJECT_STATUS_TILBUD_GODKJENT,
    PROJECT_STATUS_TILBUDSFASE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _page(
    *,
    title: str = "Acme Boligprosjekt",
    status: str | None = PROJECT_STATUS_TILBUDSFASE,
    status_shape: str = "multi_select",
) -> dict[str, Any]:
    """Build the Notion page object dispatch_project_status reads.

    Defaults to multi_select because that's Goldbox's production shape (one
    option per row). The parametrized shape test exercises plain select and
    Notion's newer `status` property type.
    """
    if status_shape == "multi_select":
        status_prop = {
            "type": "multi_select",
            "multi_select": [{"name": status}] if status else [],
        }
    elif status_shape == "select":
        status_prop = {
            "type": "select",
            "select": {"name": status} if status else None,
        }
    elif status_shape == "status":
        status_prop = {
            "type": "status",
            "status": {"name": status} if status else None,
        }
    else:
        raise ValueError(f"unknown status_shape: {status_shape}")
    return {
        "id": "proj-1",
        "properties": {
            "Navn": {
                "type": "title",
                "title": [{"plain_text": title}] if title else [],
            },
            "Status": status_prop,
        },
    }


@pytest.fixture(autouse=True)
def _wire_settings(monkeypatch):
    """Every per-engine fan-out toggle ON by default so I produksjon hits all
    four lanes. Individual tests override what they care about."""
    monkeypatch.setattr(disp.settings, "sync_gmail_labels", True, raising=False)
    monkeypatch.setattr(disp.settings, "sync_nas_folders", True, raising=False)
    monkeypatch.setattr(
        disp.settings, "nas_projects_root", "/mnt/nas/Prosjekt", raising=False
    )
    monkeypatch.setattr(disp.settings, "sync_toggl", True, raising=False)
    monkeypatch.setattr(disp.settings, "sync_frame", True, raising=False)


@pytest.fixture
def captured(monkeypatch):
    """Replace every enqueue helper + the deliverable fan-out + Notion fetches.
    Returns a dict of captures so tests can assert on what got queued."""
    calls: dict[str, list] = {
        "label_sync": [],
        "nas_folder_sync": [],
        "toggl_project_sync": [],
        "frame_project_sync": [],
        "frame_project_status_sync": [],
        "frame_deliverable_fanout": [],
    }

    async def fake_label(page_id: str) -> int:
        calls["label_sync"].append(page_id)
        return 1

    async def fake_nas(page_id: str) -> int:
        calls["nas_folder_sync"].append(page_id)
        return 1

    async def fake_toggl(page_id: str) -> int:
        calls["toggl_project_sync"].append(page_id)
        return 1

    async def fake_frame_project(page_id: str) -> int:
        calls["frame_project_sync"].append(page_id)
        return 1

    async def fake_frame_status(page_id: str) -> int:
        calls["frame_project_status_sync"].append(page_id)
        return 1

    async def fake_fanout(page_id: str) -> tuple[int, int]:
        calls["frame_deliverable_fanout"].append(page_id)
        return (2, 3)

    monkeypatch.setattr(disp, "enqueue_label_sync", fake_label)
    monkeypatch.setattr(disp, "enqueue_nas_folder_sync", fake_nas)
    monkeypatch.setattr(disp, "enqueue_toggl_project_sync", fake_toggl)
    monkeypatch.setattr(disp, "enqueue_frame_project_sync", fake_frame_project)
    monkeypatch.setattr(
        disp, "enqueue_frame_project_status_sync", fake_frame_status
    )
    monkeypatch.setattr(
        disp, "_enqueue_frame_deliverables_for_project", fake_fanout
    )
    return calls


def _patch_get_page(monkeypatch, page: dict | None) -> None:
    async def fake_get_page(page_id: str) -> dict:
        return page

    monkeypatch.setattr(disp.notion_client, "get_page", fake_get_page)


# ---------------------------------------------------------------------------
# Placeholder-title gate
# ---------------------------------------------------------------------------


def test_placeholder_title_skips_all_engines(captured, monkeypatch):
    page = _page(title="000_Kunde_Prosjekt TEMPLATE", status=PROJECT_STATUS_I_PRODUKSJON)
    _patch_get_page(monkeypatch, page)

    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.action == "skipped"
    assert result.note == "placeholder title"
    assert result.title == "000_Kunde_Prosjekt TEMPLATE"
    # No engine reached — every capture stays empty.
    for lane in captured.values():
        assert lane == []


# ---------------------------------------------------------------------------
# Status mapping (cumulative)
# ---------------------------------------------------------------------------


def test_tilbudsfase_fires_gmail_and_frame_status(captured, monkeypatch):
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_TILBUDSFASE))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.status == PROJECT_STATUS_TILBUDSFASE
    assert result.engines == ["gmail"]
    assert result.results["gmail"]["action"] == "queued"
    assert result.results["frame_status"]["action"] == "queued"
    assert "nas" not in result.results
    assert "frame" not in result.results
    assert "toggl" not in result.results

    assert captured["label_sync"] == ["proj-1"]
    assert captured["frame_project_status_sync"] == ["proj-1"]
    assert captured["nas_folder_sync"] == []
    assert captured["frame_project_sync"] == []
    assert captured["toggl_project_sync"] == []


def test_tilbud_godkjent_fires_gmail_and_nas(captured, monkeypatch):
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_TILBUD_GODKJENT))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert sorted(result.engines) == ["gmail", "nas"]
    assert captured["label_sync"] == ["proj-1"]
    assert captured["nas_folder_sync"] == ["proj-1"]
    assert captured["frame_project_sync"] == []
    assert captured["toggl_project_sync"] == []
    assert captured["frame_project_status_sync"] == ["proj-1"]


def test_i_produksjon_fires_all_four_plus_frame_status(captured, monkeypatch):
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_I_PRODUKSJON))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert sorted(result.engines) == ["frame", "gmail", "nas", "toggl"]
    assert captured["label_sync"] == ["proj-1"]
    assert captured["nas_folder_sync"] == ["proj-1"]
    assert captured["toggl_project_sync"] == ["proj-1"]
    assert captured["frame_project_sync"] == ["proj-1"]
    assert captured["frame_deliverable_fanout"] == ["proj-1"]
    assert captured["frame_project_status_sync"] == ["proj-1"]
    assert result.results["frame"]["leveranser_queued"] == 2
    assert result.results["frame"]["leveranser_total"] == 3


def test_ferdig_fires_only_frame_status(captured, monkeypatch):
    # Ferdig has no provisioning engines — but the Frame active/inactive lane
    # still fires (and inside the engine flips Frame to inactive).
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_FERDIG))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.engines == []
    assert result.status == PROJECT_STATUS_FERDIG
    assert result.results["frame_status"]["action"] == "queued"
    assert captured["frame_project_status_sync"] == ["proj-1"]
    assert captured["label_sync"] == []
    assert captured["nas_folder_sync"] == []
    assert captured["frame_project_sync"] == []
    assert captured["toggl_project_sync"] == []


def test_empty_status_fires_frame_status_only(captured, monkeypatch):
    # Clearing Status — reopens a previously-inactivated Frame project by
    # the engine's "anything other than Ferdig/Tapt → active" rule. Nothing
    # to provision.
    _patch_get_page(monkeypatch, _page(status=None))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.engines == []
    assert result.status is None
    assert result.results["frame_status"]["action"] == "queued"
    assert captured["frame_project_status_sync"] == ["proj-1"]


# ---------------------------------------------------------------------------
# Per-engine env-flag skips
# ---------------------------------------------------------------------------


def test_disabled_env_flags_skip_per_engine_but_still_run_enabled(
    captured, monkeypatch
):
    monkeypatch.setattr(disp.settings, "sync_nas_folders", False, raising=False)
    monkeypatch.setattr(disp.settings, "sync_frame", False, raising=False)
    monkeypatch.setattr(disp.settings, "sync_toggl", False, raising=False)

    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_I_PRODUKSJON))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.results["gmail"]["action"] == "queued"
    assert result.results["nas"]["action"] == "skipped"
    assert result.results["frame"]["action"] == "skipped"
    assert result.results["toggl"]["action"] == "skipped"
    # SYNC_FRAME=false ALSO turns off the active/inactive lane.
    assert result.results["frame_status"]["action"] == "skipped"
    assert result.results["frame_status"]["reason"] == "SYNC_FRAME=false"

    assert captured["label_sync"] == ["proj-1"]
    assert captured["nas_folder_sync"] == []
    assert captured["frame_project_sync"] == []
    assert captured["frame_deliverable_fanout"] == []
    assert captured["frame_project_status_sync"] == []
    assert captured["toggl_project_sync"] == []


def test_nas_skipped_when_root_unset_even_if_flag_on(captured, monkeypatch):
    monkeypatch.setattr(disp.settings, "nas_projects_root", "", raising=False)
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_TILBUD_GODKJENT))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.results["nas"]["action"] == "skipped"
    assert "disabled or unconfigured" in result.results["nas"]["reason"]
    assert captured["nas_folder_sync"] == []


# ---------------------------------------------------------------------------
# Property-shape compatibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["multi_select", "select", "status"])
def test_status_read_works_across_property_shapes(captured, monkeypatch, shape):
    _patch_get_page(
        monkeypatch, _page(status=PROJECT_STATUS_TILBUDSFASE, status_shape=shape)
    )
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.status == PROJECT_STATUS_TILBUDSFASE
    assert result.engines == ["gmail"]
    assert captured["label_sync"] == ["proj-1"]


# ---------------------------------------------------------------------------
# Idempotent re-enqueue
# ---------------------------------------------------------------------------


def test_idempotent_response_when_already_queued(captured, monkeypatch):
    async def fake_label(page_id: str) -> int:
        captured["label_sync"].append(page_id)
        return 0

    monkeypatch.setattr(disp, "enqueue_label_sync", fake_label)
    _patch_get_page(monkeypatch, _page(status=PROJECT_STATUS_TILBUDSFASE))
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.results["gmail"]["action"] == "already_queued"


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_notion_get_page_failure_marks_failed(captured, monkeypatch):
    # Transient Notion error during get_page → result.action="failed" so the
    # queue retries with backoff. We don't enqueue any sub-tasks (we don't
    # actually know the project's status).
    async def boom(_page_id: str) -> dict:
        raise RuntimeError("notion 503")

    monkeypatch.setattr(disp.notion_client, "get_page", boom)
    result = asyncio.run(disp.dispatch_project_status("proj-1"))

    assert result.action == "failed"
    assert "notion 503" in (result.note or "").lower()
    # No sub-tasks queued.
    for lane in captured.values():
        assert lane == []
