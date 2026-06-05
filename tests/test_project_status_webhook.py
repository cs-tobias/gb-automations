"""Project-status webhook (`/webhooks/notion/project-status`).

This endpoint is deliberately minimal: bearer check + parent-DB sanity +
enqueue one `project_status_dispatch` task + return 200. The actual work
(Notion fetch, placeholder gate, status mapping, per-engine fan-out, Frame
deliverable enumeration, active/inactive lane) lives on the worker and is
covered by tests/test_dispatch_project_status.py.

The split exists because Notion auto-pauses webhook automations whose
receiver takes too long to respond (see u6r7s8t9o0p1 migration docstring).
These tests pin the "fast ack" contract — nothing else.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import gb_automations.routes.webhooks as webhooks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_request(body: dict | None, *, auth: str | None = "Bearer s3cret") -> Request:
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


@pytest.fixture(autouse=True)
def _wire_secrets(monkeypatch):
    monkeypatch.setattr(webhooks.settings, "notion_webhook_secret", "s3cret", raising=False)
    monkeypatch.setattr(webhooks.settings, "projects_db_id", "projdb-uuid", raising=False)


@pytest.fixture
def captured(monkeypatch):
    """Replace `enqueue_project_status_dispatch` + `queue_worker.wake` so we can
    assert on the (single) thing this endpoint does. Anything else getting
    touched is a regression — the contract is "ack + enqueue + return."
    """
    calls: dict[str, list] = {
        "dispatch": [],
        "wake": [],
    }

    async def fake_enqueue(page_id: str) -> int:
        calls["dispatch"].append(page_id)
        return 1

    def fake_wake() -> None:
        calls["wake"].append(True)

    monkeypatch.setattr(webhooks, "enqueue_project_status_dispatch", fake_enqueue)
    monkeypatch.setattr(webhooks.queue_worker, "wake", fake_wake)

    # Hard fail if the webhook touches Notion (it must not — that's what
    # makes the response fast). A real Notion call in this path is the
    # bug we're guarding against.
    async def boom_get_page(_page_id):
        raise AssertionError(
            "webhook must not call notion.get_page — that work belongs on the worker"
        )

    monkeypatch.setattr(webhooks.notion_client, "get_page", boom_get_page)

    return calls


def _response_json(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_missing_bearer_returns_401(captured):
    req = _make_request({"data": {"id": "p"}}, auth=None)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._notion_project_status_impl(req))
    assert exc.value.status_code == 401
    assert captured["dispatch"] == []


def test_wrong_bearer_returns_401(captured):
    req = _make_request({"data": {"id": "p"}}, auth="Bearer nope")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webhooks._notion_project_status_impl(req))
    assert exc.value.status_code == 401


def test_invalid_json_returns_400(captured):
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


def test_no_page_id_skips_without_enqueue(captured):
    response = asyncio.run(
        webhooks._notion_project_status_impl(_make_request({"data": {}}))
    )
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "no page id"
    assert captured["dispatch"] == []


def test_wrong_parent_db_skips_without_enqueue(captured):
    req = _make_request(
        {"data": {"id": "p", "parent": {"database_id": "wrong-db-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "wrong parent DB"
    assert captured["dispatch"] == []


def test_valid_request_enqueues_dispatch_and_returns_immediately(captured):
    # The happy path. No Notion API call (the boom_get_page fixture pins that
    # — any reach into Notion crashes the test). Just enqueue + wake + 200.
    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)

    assert body["page_id"] == "proj-1"
    assert body["kind"] == "project_status_dispatch"
    assert body["action"] == "queued"
    assert captured["dispatch"] == ["proj-1"]
    assert captured["wake"] == [True]


def test_idempotent_response_when_already_queued(captured, monkeypatch):
    # Rapid Status flips collapse on the active-task unique index — the
    # enqueue helper returns 0. The webhook surfaces this as
    # `already_queued`, not an error.
    async def fake_enqueue(page_id: str) -> int:
        captured["dispatch"].append(page_id)
        return 0

    monkeypatch.setattr(webhooks, "enqueue_project_status_dispatch", fake_enqueue)

    req = _make_request(
        {"data": {"id": "proj-1", "parent": {"database_id": "projdb-uuid"}}}
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)
    assert body["action"] == "already_queued"


def test_parent_db_check_hyphen_and_case_insensitive(captured):
    # Notion sometimes returns the db id with hyphens, sometimes without,
    # and case can vary. The webhook normalizes both sides — pin that a
    # hyphenated, uppercase id still matches the configured `projdb-uuid`.
    req = _make_request(
        {
            "data": {
                "id": "proj-1",
                "parent": {"database_id": "PROJ-DB-UUID"},
            }
        }
    )
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    body = _response_json(response)
    assert body["action"] == "queued"
    assert captured["dispatch"] == ["proj-1"]
