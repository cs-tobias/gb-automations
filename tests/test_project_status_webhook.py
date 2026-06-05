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


def test_missing_bearer_returns_200_skipped_auth_failed(captured):
    # CRITICAL CONTRACT: the receiver MUST NOT return non-2xx — Notion
    # auto-pauses webhook automations on any non-2xx (threshold undocumented,
    # single failure can trip it). Auth failure is logged loud but Notion
    # sees a 200 with a reason in the body.
    req = _make_request({"data": {"id": "p"}}, auth=None)
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    assert response.status_code == 200
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "auth failed"
    assert captured["dispatch"] == []


def test_wrong_bearer_returns_200_skipped_auth_failed(captured):
    req = _make_request({"data": {"id": "p"}}, auth="Bearer nope")
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    assert response.status_code == 200
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "auth failed"
    assert captured["dispatch"] == []


def test_invalid_json_returns_200_skipped_invalid_json(captured):
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
    response = asyncio.run(webhooks._notion_project_status_impl(req))
    assert response.status_code == 200
    body = _response_json(response)
    assert body["action"] == "skipped"
    assert body["reason"] == "invalid JSON"
    assert captured["dispatch"] == []


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


def test_every_branch_returns_200_and_records_hit(captured, monkeypatch):
    # The "always 200" contract is the entire reason the auth/json branches
    # changed from HTTPException to _json. Pin it across every branch the
    # impl has — bad auth, bad JSON, no page id, wrong DB, happy path. Each
    # should also record a hit on _NOTION_AUTOMATION_LAST_SEEN so the
    # /debug/notion-automation-health endpoint can surface "Notion fired
    # this 47 times" without scanning logs.

    # Reset the tracker so this test's assertions are deterministic.
    webhooks._NOTION_AUTOMATION_LAST_SEEN.clear()

    cases: list[tuple[str, Request]] = [
        (
            "auth_failed",
            _make_request({"data": {"id": "p"}}, auth="Bearer nope"),
        ),
    ]

    # Bad JSON (manual scope build because _make_request only takes dicts).
    bad_json_scope = {
        "type": "http",
        "method": "POST",
        "path": "/webhooks/notion/project-status",
        "headers": [
            (b"content-type", b"application/json"),
            (b"authorization", b"Bearer s3cret"),
        ],
        "query_string": b"",
    }

    async def bad_json_receive() -> dict:
        return {"type": "http.request", "body": b"not-json{", "more_body": False}

    cases.append(("invalid_json", Request(bad_json_scope, bad_json_receive)))
    cases.append(("no_page_id", _make_request({"data": {}})))
    cases.append(
        (
            "wrong_parent_db",
            _make_request(
                {"data": {"id": "p", "parent": {"database_id": "wrong-db-uuid"}}}
            ),
        )
    )
    cases.append(
        (
            "queued",
            _make_request(
                {"data": {"id": "p1", "parent": {"database_id": "projdb-uuid"}}}
            ),
        )
    )

    for expected_action, req in cases:
        response = asyncio.run(webhooks._notion_project_status_impl(req))
        assert response.status_code == 200, (
            f"branch {expected_action!r} returned {response.status_code} "
            "— must be 200 or Notion auto-pauses the automation"
        )

    # All five branches recorded against the same automation name; counter
    # reflects every call.
    entry = webhooks._NOTION_AUTOMATION_LAST_SEEN["project-status"]
    assert entry["count"] == len(cases)
    # Last call was the happy path; last_action surfaces it.
    assert entry["last_action"] == "queued"
    assert entry["last_seen_utc"] is not None


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
