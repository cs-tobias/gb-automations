"""Unit tests for the label_sync queue path (the "Sync to Gmail" button moved
off the request path into the durable queue).

Pin the behaviors that matter without a live Postgres/Gmail/Notion:

1. `sync_project_labels` shapes its result (action / failed) from the reconcile
   + topup outcomes — first sync is "created", drift is "synced", a failing
   mailbox surfaces in `failed`.
2. The worker's `_process_label_sync` turns a non-empty `failed` into a retry
   (outcome_error set) and an all-clear into done.
3. The Notion button handler ENQUEUES and returns without running the Gmail
   engine inline (the timeout fix).
"""

from __future__ import annotations

import asyncio

import gb_automations.sync.sync_labels as sl


def _patch_engine(
    monkeypatch, *, reconcile, topup, nas="skipped", title="Acme", sync_gmail=True
):
    """Stub sync_labels' Notion fetch + the two per-user phases + NAS."""
    monkeypatch.setattr(
        sl.notion_client, "get_page", _aval({"created_time": "2026-01-01T00:00:00Z"})
    )
    monkeypatch.setattr(sl.notion_client, "extract_page_title", lambda page: title)
    monkeypatch.setattr(sl, "project_label_path", lambda t, ct: f"Prosjekt/2026/{t}")
    monkeypatch.setattr(sl.settings, "sync_gmail_labels", sync_gmail)
    monkeypatch.setattr(sl, "_reconcile_label_for_all_users", _aval(reconcile))
    monkeypatch.setattr(sl, "_create_label_for_all_users", _aval(topup))
    monkeypatch.setattr(sl, "_sync_nas_folder_for_project", _aval(nas))


def _aval(value):
    """Make an async function that ignores its args and returns `value`."""

    async def _f(*a, **k):
        return value

    return _f


def test_first_sync_is_created(monkeypatch):
    _patch_engine(
        monkeypatch,
        reconcile={"patched": [], "healed": [], "unchanged": [], "failed": [], "no_mapping": True},
        topup={"created": ["a@x.no", "b@x.no"], "already_present": [], "failed": []},
    )
    result = asyncio.run(sl.sync_project_labels("p1"))
    assert result.action == "created"
    assert result.created == ["a@x.no", "b@x.no"]
    assert result.failed == []
    assert result.label_name == "Prosjekt/2026/Acme"


def test_drift_is_synced(monkeypatch):
    _patch_engine(
        monkeypatch,
        reconcile={"patched": ["a@x.no"], "healed": [], "unchanged": ["b@x.no"], "failed": []},
        topup={"created": [], "already_present": ["a@x.no", "b@x.no"], "failed": []},
    )
    result = asyncio.run(sl.sync_project_labels("p1"))
    assert result.action == "synced"
    assert result.patched == ["a@x.no"]


def test_failing_mailbox_surfaces_in_failed(monkeypatch):
    _patch_engine(
        monkeypatch,
        reconcile={"patched": [], "healed": [], "unchanged": ["a@x.no"], "failed": ["b@x.no"]},
        topup={"created": [], "already_present": ["a@x.no"], "failed": []},
    )
    result = asyncio.run(sl.sync_project_labels("p1"))
    assert result.failed == ["b@x.no"]


def test_no_title_is_skipped(monkeypatch):
    _patch_engine(monkeypatch, reconcile={}, topup={}, title=None)
    result = asyncio.run(sl.sync_project_labels("p1"))
    assert result.action == "skipped"
    assert result.note == "no title yet"


# --------------------------------------------------------------------------
# Worker dispatch: failed mailbox -> retry, all-clear -> done
# --------------------------------------------------------------------------


def test_worker_label_sync_failure_triggers_retry(monkeypatch):
    import gb_automations.jobs.queue_worker as qw

    recorded: dict = {}

    async def fake_sync(page_id):
        return sl.LabelSyncResult(project_page_id=page_id, action="synced", failed=["b@x.no"])

    async def fake_record(task_id, attempts, outcome_error, *, progress, label):
        recorded["outcome_error"] = outcome_error
        return outcome_error is None

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(qw, "sync_project_labels", fake_sync)
    monkeypatch.setattr(qw, "_record_outcome", fake_record)
    monkeypatch.setattr(qw.queue_mirror, "refresh_project_dot", noop)

    claimed = qw._Claimed(
        id=1, task_type="label_sync", user_email="*", gmail_thread_id="p1",
        project_page_id="p1", attempts=1, rebuild=False,
    )
    asyncio.run(qw._process_label_sync(claimed, "1/1"))
    # A failed mailbox must produce an error string so _record_outcome retries.
    assert recorded["outcome_error"] is not None
    assert "b@x.no" in recorded["outcome_error"]


def test_worker_label_sync_clean_marks_done(monkeypatch):
    import gb_automations.jobs.queue_worker as qw

    recorded: dict = {}

    async def fake_sync(page_id):
        return sl.LabelSyncResult(project_page_id=page_id, action="created", failed=[])

    async def fake_record(task_id, attempts, outcome_error, *, progress, label):
        recorded["outcome_error"] = outcome_error
        return outcome_error is None

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(qw, "sync_project_labels", fake_sync)
    monkeypatch.setattr(qw, "_record_outcome", fake_record)
    monkeypatch.setattr(qw.queue_mirror, "refresh_project_dot", noop)

    claimed = qw._Claimed(
        id=1, task_type="label_sync", user_email="*", gmail_thread_id="p1",
        project_page_id="p1", attempts=1, rebuild=False,
    )
    asyncio.run(qw._process_label_sync(claimed, "1/1"))
    assert recorded["outcome_error"] is None


# --------------------------------------------------------------------------
# ON CONFLICT predicate must match the partial unique index predicate, or
# Postgres errors "no unique or exclusion constraint matching the ON CONFLICT
# specification" at runtime (the regression that broke Gmail thread enqueue
# when the index gained `AND task_type = 'thread'`). Pure-SQL assertion, no DB.
# --------------------------------------------------------------------------


def _index_predicate(index_name: str) -> str:
    from gb_automations.models import SyncTask

    for idx in SyncTask.__table__.indexes:
        if idx.name == index_name:
            return str(idx.dialect_options["postgresql"]["where"])
    raise AssertionError(f"index {index_name} not found")


def test_enqueue_thread_conflict_matches_index():
    import gb_automations.sync.queue as q

    assert str(q._ACTIVE_THREAD_PREDICATE) == _index_predicate("uq_sync_tasks_active_thread")


def test_enqueue_label_conflict_matches_index():
    import gb_automations.sync.queue as q

    assert str(q._ACTIVE_LABEL_PREDICATE) == _index_predicate("uq_sync_tasks_active_label")
