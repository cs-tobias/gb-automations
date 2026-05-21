"""Unit tests for the durable sync queue's pure decision logic.

These pin the behaviors that guarantee "a labeled thread WILL reach Notion"
without a live Postgres/Gmail/Notion:

1. `list_history` follows pagination so a bulk label producing >100 history
   records is fully captured (the silent-truncation bug).
2. `mark_failed` retries below the cap (status back to pending, backoff applied)
   and parks terminally at the cap (status failed).
3. `enqueue_missing_for_all_projects` only enqueues threads NOT already covered
   by a done/failed task row.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import gb_automations.clients.gmail as gmail
import gb_automations.sync.queue as q
import gb_automations.sync.resync_project as rp

# --------------------------------------------------------------------------
# 1. list_history pagination
# --------------------------------------------------------------------------


class _FakeHistoryList:
    """Mimics service.users().history().list(...).execute() across pages."""

    def __init__(self, pages):
        self._pages = pages

    def list(self, **kwargs):
        token = kwargs.get("pageToken")
        # Map token -> page index; None means first page.
        idx = 0 if token is None else int(token)
        page = self._pages[idx]
        return SimpleNamespace(execute=lambda: page)


def _build_service(pages):
    history = _FakeHistoryList(pages)
    users = SimpleNamespace(history=lambda: history)
    return SimpleNamespace(users=lambda: users)


def test_list_history_follows_pagination(monkeypatch):
    # Three pages of history; only the last carries no nextPageToken and the
    # newest historyId. All entries must be merged, newest historyId returned.
    pages = [
        {"history": [{"id": "h1"}, {"id": "h2"}], "nextPageToken": "1", "historyId": "100"},
        {"history": [{"id": "h3"}], "nextPageToken": "2", "historyId": "150"},
        {"history": [{"id": "h4"}], "historyId": "200"},  # no token = last
    ]
    monkeypatch.setattr(gmail, "gmail_for", lambda email: _build_service(pages))

    merged = gmail.list_history("user@x.no", "50")

    assert [h["id"] for h in merged["history"]] == ["h1", "h2", "h3", "h4"]
    assert merged["historyId"] == "200"  # newest cursor wins


def test_list_history_respects_max_pages(monkeypatch):
    # A pathological never-ending token chain must stop at max_pages, not spin.
    def make_page(i):
        return {"history": [{"id": f"h{i}"}], "nextPageToken": str(i + 1), "historyId": str(i)}

    pages = [make_page(i) for i in range(100)]
    monkeypatch.setattr(gmail, "gmail_for", lambda email: _build_service(pages))

    merged = gmail.list_history("user@x.no", "0", max_results=1, max_pages=3)

    assert len(merged["history"]) == 3  # capped


# --------------------------------------------------------------------------
# 2. mark_failed retry vs terminal
# --------------------------------------------------------------------------


class _CapturingSession:
    """Records the literal SET values of each update statement passed to execute.

    Compiling the statement resolves the bound params to their literal values,
    which is more robust than poking at the pre-compile `_values` placeholders.
    """

    def __init__(self):
        self.updates: list[dict] = []

    async def execute(self, stmt):
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        self.updates.append(compiled.params)
        # Re-extract the SET column names → literal values from the compiled SQL
        # params by mapping update column order; params dict keys are the param
        # names, values are the literals. Store the raw text too for assertions.
        self.updates[-1]["__sql__"] = str(compiled)
        return SimpleNamespace(rowcount=1)


def test_mark_failed_retries_below_cap():
    session = _CapturingSession()
    task = SimpleNamespace(id=1, attempts=2)  # 2nd attempt, cap 5 -> retry

    terminal = asyncio.run(
        q.mark_failed(session, task, "boom", max_attempts=5, base_backoff_seconds=30)
    )

    assert terminal is False
    sql = session.updates[-1]["__sql__"]
    assert "status='pending'" in sql.replace('"', "").replace(" = ", "=")
    assert "next_attempt_at" in sql  # backoff scheduled


def test_mark_failed_parks_terminally_at_cap():
    session = _CapturingSession()
    task = SimpleNamespace(id=1, attempts=5)  # reached cap

    terminal = asyncio.run(
        q.mark_failed(session, task, "boom", max_attempts=5, base_backoff_seconds=30)
    )

    assert terminal is True
    sql = session.updates[-1]["__sql__"]
    assert "status='failed'" in sql.replace('"', "").replace(" = ", "=")


# --------------------------------------------------------------------------
# 3. enqueue_missing_for_all_projects subtracts covered threads
# --------------------------------------------------------------------------


def test_enqueue_missing_skips_covered_threads(monkeypatch):
    enqueued: list[tuple[str, list[str]]] = []

    async def fake_list_projects():
        return [rp.ProjectRef(page_id="p1", name="Prosjekt/2026/Acme")]

    async def fake_resolve(page_id, only_user):
        return [("alice@x.no", "Prosjekt/2026/Acme")]

    async def fake_enumerate(labels):
        # three threads under the label; t1 already synced, t2 terminally failed
        return {"t1": "alice@x.no", "t2": "alice@x.no", "t3": "alice@x.no"}

    monkeypatch.setattr(rp, "list_projects", fake_list_projects)
    monkeypatch.setattr(rp, "_resolve_labels", fake_resolve)
    monkeypatch.setattr(rp, "_enumerate_threads", fake_enumerate)

    # Fake the "already covered" DB read: t1 done, t2 failed -> only t3 missing.
    class _Result:
        def scalars(self):
            return ["t1", "t2"]

    class _Session:
        async def execute(self, stmt):
            return _Result()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(rp, "SessionLocal", lambda: _Session())

    async def fake_enqueue(owner, tids):
        enqueued.append((owner, list(tids)))
        return len(tids)

    # enqueue_threads is imported lazily inside the function from sync.queue.
    monkeypatch.setattr("gb_automations.sync.queue.enqueue_threads", fake_enqueue)

    total = asyncio.run(rp.enqueue_missing_for_all_projects())

    assert total == 1
    assert enqueued == [("alice@x.no", ["t3"])]
