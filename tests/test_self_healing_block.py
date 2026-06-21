"""Tests for the self-healing draft-in-flight block check.

Background: send_faktura blocks a click when a project has at least one
unsent FikenInvoice row. That row gets stuck if the operator deletes
the draft directly in Fiken's UI — Postgres still says "in flight"
even though Fiken doesn't.

Fix: the block check verifies each candidate against Fiken before
counting it. Stale rows get sent_at stamped and dropped from the
in-flight set; live rows still block; Fiken errors fail-safe (still
block, so we don't accidentally create duplicate drafts during a
Fiken outage).

These tests pin the dispatch logic by mocking the Postgres SELECT
side AND the fiken `check_draft_exists` side, then asserting which
candidates end up in the live-ids set and which get marked stale.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gb_automations.sync import sync_fiken_invoice as engine


class _FakeScalars:
    """Mimics the `scalars` interface SQLAlchemy returns from execute()."""

    def __init__(self, values: list[str]):
        self._values = values

    def __iter__(self):
        return iter(self._values)


class _FakeResult:
    def __init__(self, values: list[str]):
        self._values = values

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._values)


class _FakeSession:
    """Minimal async session stand-in: execute() returns a FakeResult
    whose scalars() yields a hardcoded list of fiken_invoice_ids."""

    def __init__(self, candidates: list[str]):
        self._candidates = candidates

    async def execute(self, _stmt) -> _FakeResult:
        return _FakeResult(self._candidates)

    async def commit(self) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _patch_session(monkeypatch: pytest.MonkeyPatch, candidates: list[str]) -> None:
    """Replace engine.SessionLocal() with a factory returning a
    FakeSession that yields the given candidate fiken_invoice_ids."""

    def _factory():
        return _FakeSession(candidates)

    monkeypatch.setattr(engine, "SessionLocal", _factory)


@pytest.mark.asyncio
async def test_live_set_drops_drafts_that_fiken_says_are_gone(
    monkeypatch: pytest.MonkeyPatch,
):
    """A candidate row whose draft Fiken returns 404 for is dropped
    from the live set AND marked sent (cleanup writeback)."""
    _patch_session(monkeypatch, ["draft-A", "draft-B"])

    async def fake_check(_slug: str, draft_id: str) -> str:
        return "exists" if draft_id == "draft-A" else "gone"

    mark_calls: list[str] = []

    async def fake_mark(_slug: str, draft_id: str) -> None:
        mark_calls.append(draft_id)

    monkeypatch.setattr(
        engine.fiken_client, "check_draft_exists", fake_check
    )
    monkeypatch.setattr(engine, "_mark_audit_row_stale", fake_mark)

    live = await engine._live_unsent_invoice_ids_for_project(
        "cinesuit-as", "proj-1"
    )
    assert live == {"draft-A"}
    assert mark_calls == ["draft-B"]


@pytest.mark.asyncio
async def test_live_set_fails_safe_on_unknown(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fiken-side error (5xx, network, etc.) for a candidate must
    count it toward the block — we don't accidentally unblock a real
    in-flight draft during an outage. No cleanup writeback in that case.
    """
    _patch_session(monkeypatch, ["draft-X"])

    async def fake_check(_slug: str, _draft_id: str) -> str:
        return "unknown"

    mark_calls: list[str] = []

    async def fake_mark(_slug: str, draft_id: str) -> None:
        mark_calls.append(draft_id)

    monkeypatch.setattr(
        engine.fiken_client, "check_draft_exists", fake_check
    )
    monkeypatch.setattr(engine, "_mark_audit_row_stale", fake_mark)

    live = await engine._live_unsent_invoice_ids_for_project(
        "cinesuit-as", "proj-1"
    )
    assert live == {"draft-X"}
    assert mark_calls == []  # NOT marked — we don't know it's gone


@pytest.mark.asyncio
async def test_live_set_empty_when_no_candidates_in_postgres(
    monkeypatch: pytest.MonkeyPatch,
):
    """Empty Postgres → empty live set without any Fiken calls."""
    _patch_session(monkeypatch, [])

    check = AsyncMock()
    mark = AsyncMock()
    monkeypatch.setattr(engine.fiken_client, "check_draft_exists", check)
    monkeypatch.setattr(engine, "_mark_audit_row_stale", mark)

    live = await engine._live_unsent_invoice_ids_for_project(
        "cinesuit-as", "proj-1"
    )
    assert live == set()
    check.assert_not_called()
    mark.assert_not_called()


@pytest.mark.asyncio
async def test_project_has_unsent_draft_returns_False_when_all_stale(
    monkeypatch: pytest.MonkeyPatch,
):
    """Top-level wrapper: when every candidate's draft is gone in
    Fiken, the block check returns False (no block) and the stale
    rows are cleaned up."""
    _patch_session(monkeypatch, ["stuck-1", "stuck-2"])

    async def fake_check(_slug: str, _draft_id: str) -> str:
        return "gone"

    mark_calls: list[str] = []

    async def fake_mark(_slug: str, draft_id: str) -> None:
        mark_calls.append(draft_id)

    monkeypatch.setattr(
        engine.fiken_client, "check_draft_exists", fake_check
    )
    monkeypatch.setattr(engine, "_mark_audit_row_stale", fake_mark)

    blocked = await engine._project_has_unsent_draft(
        "cinesuit-as", "proj-1", invoice_type="oppstart"
    )
    assert blocked is False
    assert sorted(mark_calls) == ["stuck-1", "stuck-2"]


@pytest.mark.asyncio
async def test_project_has_unsent_draft_returns_True_on_live_draft(
    monkeypatch: pytest.MonkeyPatch,
):
    """One live, one stale → block fires (True), and the stale one
    still gets cleaned up so future runs don't have to re-verify it.
    """
    _patch_session(monkeypatch, ["live-A", "stale-B"])

    async def fake_check(_slug: str, draft_id: str) -> str:
        return "exists" if draft_id == "live-A" else "gone"

    mark_calls: list[str] = []

    async def fake_mark(_slug: str, draft_id: str) -> None:
        mark_calls.append(draft_id)

    monkeypatch.setattr(
        engine.fiken_client, "check_draft_exists", fake_check
    )
    monkeypatch.setattr(engine, "_mark_audit_row_stale", fake_mark)

    blocked = await engine._project_has_unsent_draft(
        "cinesuit-as", "proj-1", invoice_type="oppstart"
    )
    assert blocked is True
    assert mark_calls == ["stale-B"]
