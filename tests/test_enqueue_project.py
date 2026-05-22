"""Tests for resync_project.enqueue_project — the queue-based project resync
behind the Notion "Resync Project" button.

Pins: it resolves the project's threads, groups them by owning mailbox, and
enqueues each group with the rebuild flag — without touching Notion or syncing
inline. Mirrors the mock-the-boundaries style of test_sync_queue.py.
"""

from __future__ import annotations

import asyncio

import gb_automations.sync.resync_project as rp


def test_enqueue_project_groups_by_owner_and_flags_rebuild(monkeypatch):
    enqueued: list[tuple[str, list[str], bool]] = []

    async def fake_resolve_labels(page_id, only_user):
        assert page_id == "proj-1"
        return [("alice@x.no", "Prosjekt/2026/Acme"), ("bob@x.no", "Prosjekt/2026/Acme")]

    async def fake_enumerate(labels):
        # t1/t2 owned by alice, t3 by bob — two owners to group.
        return {"t1": "alice@x.no", "t2": "alice@x.no", "t3": "bob@x.no"}

    async def fake_enqueue(owner, thread_ids, *, rebuild=False):
        enqueued.append((owner, sorted(thread_ids), rebuild))
        return len(list(thread_ids))

    monkeypatch.setattr(rp, "_resolve_labels", fake_resolve_labels)
    monkeypatch.setattr(rp, "_enumerate_threads", fake_enumerate)
    monkeypatch.setattr(rp, "enqueue_threads", fake_enqueue)

    summary = asyncio.run(rp.enqueue_project("proj-1", rebuild=True))

    assert summary == {"threads": 3, "enqueued": 3, "labels": 2}
    # One enqueue call per owner, each carrying that owner's threads + rebuild.
    by_owner = {owner: (tids, rebuild) for owner, tids, rebuild in enqueued}
    assert by_owner["alice@x.no"] == (["t1", "t2"], True)
    assert by_owner["bob@x.no"] == (["t3"], True)


def test_enqueue_project_no_threads(monkeypatch):
    async def fake_resolve_labels(page_id, only_user):
        return [("alice@x.no", "Prosjekt/2026/Empty")]

    async def fake_enumerate(labels):
        return {}

    async def fake_enqueue(owner, thread_ids, *, rebuild=False):  # pragma: no cover
        raise AssertionError("must not enqueue when there are no threads")

    monkeypatch.setattr(rp, "_resolve_labels", fake_resolve_labels)
    monkeypatch.setattr(rp, "_enumerate_threads", fake_enumerate)
    monkeypatch.setattr(rp, "enqueue_threads", fake_enqueue)

    summary = asyncio.run(rp.enqueue_project("proj-empty"))

    assert summary == {"threads": 0, "enqueued": 0, "labels": 1}


def test_enqueue_project_reports_partial_enqueue(monkeypatch):
    # When some threads already have an active queue row, enqueue_threads returns
    # fewer than passed — the summary must reflect that (idempotency surfaced).
    async def fake_resolve_labels(page_id, only_user):
        return [("alice@x.no", "Prosjekt/2026/Acme")]

    async def fake_enumerate(labels):
        return {"t1": "alice@x.no", "t2": "alice@x.no"}

    async def fake_enqueue(owner, thread_ids, *, rebuild=False):
        return 1  # only one was newly inserted; the other was already queued

    monkeypatch.setattr(rp, "_resolve_labels", fake_resolve_labels)
    monkeypatch.setattr(rp, "_enumerate_threads", fake_enumerate)
    monkeypatch.setattr(rp, "enqueue_threads", fake_enqueue)

    summary = asyncio.run(rp.enqueue_project("proj-1"))

    assert summary == {"threads": 2, "enqueued": 1, "labels": 1}
