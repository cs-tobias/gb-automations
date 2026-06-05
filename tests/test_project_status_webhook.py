"""Project-status auto-provision webhook (`/webhooks/notion/project-status`).

Pins the dispatch shape that turns a Notion project Status change into the
right set of queued provisioning tasks. Mocks the boundaries (bearer verify,
notion fetch, enqueue helpers, queue_worker.wake) the same way
test_enqueue_project.py / test_sync_queue.py do — no FastAPI TestClient, no
database — so the tests are unit-fast.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import gb_automations.routes.webhooks as webhooks
from gb_automations.config import (
    PROJECT_STATUS_FERDIG,
    PROJECT_STATUS_I_PRODUKSJON,
    PROJECT_STATUS_TILBUD_GODKJENT,
    PROJECT_STATUS_TILBUDSFASE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_request(body: dict | None, *, auth: str | None = "Bearer s3cret") -> Request:
    """Build a minimal Starlette Request carrying `body` (JSON) and an
    Authorization header. Enough for the impl's `await request.body()` +
    header read; nothing else in the handler touches the request.
    """
    raw = json.dumps(body).encode("utf-8") if body is not None else b""
    headers: list[tuple[bytes, bytes]] = [(b"content-type", b"application/json")]
    if auth is not None:
        headers.append((b"authorization", auth.encode("utf-8")))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/notion/project-status",
        "headers": headers,
        "query_string": b"",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


def _page(
    *,
    page_id: str = "proj-1",
    parent_db: str = "projdb-uuid",
    title: str = "Acme Boligprosjekt",
    status: str | None = PROJECT_STATUS_TILBUDSFASE,
    status_shape: str = "multi_select",
) -> dict[str, Any]:
    """Shape the Notion page object the impl reads via get_page.

    `status_shape` flips between Notion's three Status-property variants so
    each branch of extract_project_status gets exercised. Defaults to
    `multi_select` because that's how Goldbox configures it in production
    (the same shape `task_discipline` already handles on Oppgaver).
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
    props: dict[str, Any] = {
        "Navn": {
            "type": "title",
            "title": [{"plain_text": title}] if title else [],
        },
        "Status": status_prop,
    }
    return {
        "id": page_id,
        "parent": {"type": "database_id", "database_id": parent_db},
        "properties": props,
    }


@pytest.fixture(autouse=True)
def _wire_secrets_and_db(monkeypatch):
    """Default env shape every test inherits: bearer secret set, Projects DB
    id pinned so the parent check is on, every per-engine fan-out toggle ON
    so the cumulative I produksjon path is exercised by default. Individual
    tests override what they care about.
    """
    monkeypatch.setattr(webhooks.settings, "notion_webhook_secret", "s3cret", raising=False)
    monkeypatch.setattr(webhooks.settings, "projects_db_id", "projdb-uuid", raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_gmail_labels", True, raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_nas_folders", True, raising=False)
    monkeypatch.setattr(webhooks.settings, "nas_projects_root", "/mnt/nas/Prosjekt", raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_toggl", True, raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_frame", True, raising=False)


@pytest.fixture
def captured_enqueues(monkeypatch):
    """Replace every enqueue helper + queue_worker.wake the impl reaches for,
    capturing each call. Returns the capture dict so tests can assert on it.
    """
    calls: dict[str, list] = {
        "label_sync": [],
        "nas_folder_sync": [],
        "toggl_project_sync": [],
        "frame_project_sync": [],
        "frame_deliverable_fanout": [],
        "wake": [],
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

    async def fake_fanout(page_id: str) -> tuple[int, int]:
        calls["frame_deliverable_fanout"].append(page_id)
        return (2, 3)

    def fake_wake() -> None:
        calls["wake"].append(True)

    monkeypatch.setattr(webhooks, "enqueue_label_sync", fake_label)
    monkeypatch.setattr(webhooks, "enqueue_nas_folder_sync", fake_nas)
    monkeypatch.setattr(webhooks, "enqueue_toggl_project_sync", fake_toggl)
    monkeypatch.setattr(webhooks, "enqueue_frame_project_sync", fake_frame_project)
    monkeypatch.setattr(
        webhooks,
        "_enqueue_frame_deliverables_for_project",
        fake_fanout,
    )
    monkeypatch.setattr(webhooks.queue_worker, "wake", fake_wake)
    return calls


def _patch_get_page(monkeypatch, page: dict[str, Any] | None) -> None:
    async def fake_get_page(page_id: str) -> dict[str, Any]:
        return page

    monkeypatch.setattr(webhooks.notion_client, "get_page", fake_get_page)


def _response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Auth + payload-shape guards
# ---------------------------------------------------------------------------


def test_missing_bearer_returns_401(captured_enqueues):
    req = _make_request({"data": {"id": "p"}}, auth=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._notion_project_status_impl(req))
    assert exc.value.status_code == 401
    # No engines reached, no wake.
    assert all(not v for v in captured_enqueues.values())


def test_wrong_bearer_returns_401(captured_enqueues):
    req = _make_request({"data": {"id": "p"}}, auth="Bearer nope")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._notion_project_status_impl(req))
    assert exc.value.status_code == 401


def test_invalid_json_returns_400(captured_enqueues):
    # Build a request with a non-JSON body manually.
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/notion/project-status",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer s3cret"),
        ],
        "query_string": b"",
    }

    async def receive() -> dict:
        return {"type": "http.request", "body": b"not-json{", "more_body": False}

    req = Request(scope, receive)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._notion_project_status_impl(req))
    assert exc.value.status_code == 400


def test_no_page_id_skips_without_fetch(captured_enqueues, monkeypatch):
    # If get_page is reached, this raises — pins that we shortcut earlier.
    async def boom(_):
        raise AssertionError("must not fetch when there is no page id")

    monkeypatch.setattr(webhooks.notion_client, "get_page", boom)

    response = asyncio.run(
        webhooks._notion_project_status_impl(_make_request({"data": {}}))
    )
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "no page id"


def test_wrong_parent_db_skips(captured_enqueues, monkeypatch):
    async def boom(_):
        raise AssertionError("must not fetch on wrong parent DB")

    monkeypatch.setattr(webhooks.notion_client, "get_page", boom)

    req = _make_request(
        {"data": {"id": "p", "parent": {"database_id": "wrong-db-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "wrong parent DB"


# ---------------------------------------------------------------------------
# Placeholder-title gate
# ---------------------------------------------------------------------------


def test_placeholder_title_skips_all_engines(captured_enqueues, monkeypatch):
    page = _page(title="000_Kunde_Prosjekt TEMPLATE", status=PROJECT_STATUS_I_PRODUKSJON)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["action"] == "skipped"
    assert body["reason"] == "placeholder title"
    # Title is reported back so the operator can see what we matched against.
    assert body["title"] == "000_Kunde_Prosjekt TEMPLATE"
    # No engine reached; no wake.
    assert captured_enqueues["label_sync"] == []
    assert captured_enqueues["nas_folder_sync"] == []
    assert captured_enqueues["toggl_project_sync"] == []
    assert captured_enqueues["frame_project_sync"] == []
    assert captured_enqueues["frame_deliverable_fanout"] == []
    assert captured_enqueues["wake"] == []


# ---------------------------------------------------------------------------
# Status → engine mapping (cumulative)
# ---------------------------------------------------------------------------


def test_tilbudsfase_fires_only_gmail(captured_enqueues, monkeypatch):
    page = _page(status=PROJECT_STATUS_TILBUDSFASE)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["status"] == PROJECT_STATUS_TILBUDSFASE
    assert body["engines"] == ["gmail"]
    assert body["results"]["gmail"]["action"] == "queued"
    assert "nas" not in body["results"]
    assert "frame" not in body["results"]
    assert "toggl" not in body["results"]

    assert captured_enqueues["label_sync"] == ["proj-1"]
    assert captured_enqueues["nas_folder_sync"] == []
    assert captured_enqueues["frame_project_sync"] == []
    assert captured_enqueues["toggl_project_sync"] == []
    assert captured_enqueues["wake"] == [True]


def test_tilbud_godkjent_fires_gmail_and_nas(captured_enqueues, monkeypatch):
    page = _page(status=PROJECT_STATUS_TILBUD_GODKJENT)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert sorted(body["engines"]) == ["gmail", "nas"]
    assert captured_enqueues["label_sync"] == ["proj-1"]
    assert captured_enqueues["nas_folder_sync"] == ["proj-1"]
    assert captured_enqueues["frame_project_sync"] == []
    assert captured_enqueues["toggl_project_sync"] == []


def test_i_produksjon_fires_all_four(captured_enqueues, monkeypatch):
    page = _page(status=PROJECT_STATUS_I_PRODUKSJON)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert sorted(body["engines"]) == ["frame", "gmail", "nas", "toggl"]
    assert captured_enqueues["label_sync"] == ["proj-1"]
    assert captured_enqueues["nas_folder_sync"] == ["proj-1"]
    assert captured_enqueues["toggl_project_sync"] == ["proj-1"]
    assert captured_enqueues["frame_project_sync"] == ["proj-1"]
    # Frame fan-out was invoked; the captured (queued, total) is surfaced.
    assert captured_enqueues["frame_deliverable_fanout"] == ["proj-1"]
    assert body["results"]["frame"]["leveranser_queued"] == 2
    assert body["results"]["frame"]["leveranser_total"] == 3


def test_unmapped_status_skips(captured_enqueues, monkeypatch):
    # Ferdig / Tapt / Lang pause / Klar til oppstart / Venter på avklaring are
    # explicit no-ops today — recognized, but no engine in the auto-provision
    # map.
    page = _page(status=PROJECT_STATUS_FERDIG)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["action"] == "skipped"
    assert body["reason"] == "status not mapped"
    assert body["status"] == PROJECT_STATUS_FERDIG
    assert all(not v for k, v in captured_enqueues.items())


def test_empty_status_skips(captured_enqueues, monkeypatch):
    page = _page(status=None)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["action"] == "skipped"
    assert body["reason"] == "status not mapped"
    assert body["status"] is None


# ---------------------------------------------------------------------------
# Per-engine env-flag skips
# ---------------------------------------------------------------------------


def test_disabled_env_flags_skip_per_engine_but_still_run_enabled(
    captured_enqueues, monkeypatch
):
    # I produksjon would normally fan out to all four; turn three of them off
    # via env and pin that the impl reports each as `skipped: <reason>` while
    # still queueing the one that IS enabled (gmail in this case).
    monkeypatch.setattr(webhooks.settings, "sync_nas_folders", False, raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_frame", False, raising=False)
    monkeypatch.setattr(webhooks.settings, "sync_toggl", False, raising=False)

    page = _page(status=PROJECT_STATUS_I_PRODUKSJON)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["results"]["gmail"]["action"] == "queued"
    assert body["results"]["nas"]["action"] == "skipped"
    assert body["results"]["frame"]["action"] == "skipped"
    assert body["results"]["toggl"]["action"] == "skipped"
    # Only the gmail enqueue actually ran.
    assert captured_enqueues["label_sync"] == ["proj-1"]
    assert captured_enqueues["nas_folder_sync"] == []
    assert captured_enqueues["frame_project_sync"] == []
    assert captured_enqueues["frame_deliverable_fanout"] == []
    assert captured_enqueues["toggl_project_sync"] == []


def test_nas_skipped_when_root_unset_even_if_flag_on(
    captured_enqueues, monkeypatch
):
    # nas_projects_root is the second half of the NAS gate — without a mount
    # path, sync_nas_folders=true is still inert. Mirrors _sync_nas_impl.
    monkeypatch.setattr(webhooks.settings, "nas_projects_root", "", raising=False)

    page = _page(status=PROJECT_STATUS_TILBUD_GODKJENT)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["results"]["nas"]["action"] == "skipped"
    assert "disabled or unconfigured" in body["results"]["nas"]["reason"]
    assert captured_enqueues["nas_folder_sync"] == []


@pytest.mark.parametrize("shape", ["multi_select", "select", "status"])
def test_status_read_works_across_property_shapes(
    captured_enqueues, monkeypatch, shape
):
    # Goldbox configures Status as a multi_select (one option per row); other
    # workspaces may use plain select or Notion's `status` property type.
    # extract_project_status reads all three — pin that the dispatch path
    # behaves the same regardless of how the column is configured in Notion.
    page = _page(status=PROJECT_STATUS_TILBUDSFASE, status_shape=shape)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["status"] == PROJECT_STATUS_TILBUDSFASE
    assert body["engines"] == ["gmail"]
    assert captured_enqueues["label_sync"] == ["proj-1"]


def test_idempotent_response_when_already_queued(captured_enqueues, monkeypatch):
    # When an enqueue helper returns 0 (active task already exists for this
    # project), the impl reports `already_queued` rather than `queued` — so
    # the response surface tells the operator whether a click was a no-op.
    async def fake_label(page_id: str) -> int:
        captured_enqueues["label_sync"].append(page_id)
        return 0

    monkeypatch.setattr(webhooks, "enqueue_label_sync", fake_label)

    page = _page(status=PROJECT_STATUS_TILBUDSFASE)
    _patch_get_page(monkeypatch, page)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["results"]["gmail"]["action"] == "already_queued"
