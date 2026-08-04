"""Unit tests for the hourly Fiken-graduation poller's enqueue eligibility.

`_enqueue_fiken_graduations_for_all_active_projects` (jobs/scheduler.py) is the
cron that enqueues a `graduate_faktura` task per project whose `Faktura status`
is NON-terminal. The bug these tests lock down: `Oppstart fakturert` (the
engine-written resting state after a 50% invoice graduates) was missing from the
`terminal` set, so every 50%-billed project got re-enqueued EVERY HOUR forever —
each re-scanning Fiken's full invoice catalogue for matched=0. See gotchas.md §19.

Contract pinned here:
  - Operator-INTENT statuses (Til …) are the only ones that get enqueued.
  - Engine-written resting states (Oppstart fakturert / Fakturert / Kreditert),
    blank, and Ikke fakturert are terminal → skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import gb_automations.jobs.scheduler as sched
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    FAKTURA_STATUS_50,
    FAKTURA_STATUS_FULL,
    FAKTURA_STATUS_IKKE,
    FAKTURA_STATUS_KREDITERT,
    FAKTURA_STATUS_TIL_AVSLUTNING,
    FAKTURA_STATUS_TIL_FAKTURERING,
    FAKTURA_STATUS_TIL_KREDITERING,
    FAKTURA_STATUS_TIL_OPPSTART,
    PROJECTS_FAKTURA_STATUS_PROP,
)


def _project_row(page_id: str, status: str | None) -> dict[str, Any]:
    """A Projects DB row shaped the way read_select_name reads it.

    status=None → the property is present but empty (operator cleared it),
    which the cron must treat as terminal.
    """
    if status is None:
        select_val: dict[str, Any] | None = None
    else:
        select_val = {"name": status}
    return {
        "id": page_id,
        "properties": {
            PROJECTS_FAKTURA_STATUS_PROP: {"select": select_val},
        },
    }


async def _run_cron_over(
    monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> list[str]:
    """Drive the cron over `rows`, returning the page_ids it enqueued."""
    enqueued_ids: list[str] = []

    async def _fake_query_database(_db_id: str) -> list[dict[str, Any]]:
        return rows

    async def _fake_enqueue(page_id: str) -> int:
        enqueued_ids.append(page_id)
        return 1  # 1 = newly enqueued

    # settings.projects_db_id must be truthy or the cron early-returns.
    monkeypatch.setattr(sched.settings, "projects_db_id", "db-123", raising=False)
    monkeypatch.setattr(notion_client, "query_database", _fake_query_database)
    # The cron imports enqueue_graduate_faktura from sync.queue INSIDE the
    # function; patch it at the source module so the local import binds the fake.
    import gb_automations.sync.queue as queue_mod

    monkeypatch.setattr(queue_mod, "enqueue_graduate_faktura", _fake_enqueue)
    # wake() would try to poke a non-running worker; no-op it.
    monkeypatch.setattr(sched.queue_worker, "wake", lambda: None)

    await sched._enqueue_fiken_graduations_for_all_active_projects()
    return enqueued_ids


# ---------------------------------------------------------------------------
# The eligibility contract, one status per case
# ---------------------------------------------------------------------------

TERMINAL_STATUSES = [
    None,
    "",
    FAKTURA_STATUS_IKKE,
    FAKTURA_STATUS_50,  # the regression guard: engine-written 50% resting state
    FAKTURA_STATUS_FULL,
    FAKTURA_STATUS_KREDITERT,
]

ACTIVE_STATUSES = [
    FAKTURA_STATUS_TIL_OPPSTART,
    FAKTURA_STATUS_TIL_AVSLUTNING,
    FAKTURA_STATUS_TIL_FAKTURERING,
    FAKTURA_STATUS_TIL_KREDITERING,
]


@pytest.mark.parametrize("status", TERMINAL_STATUSES)
def test_terminal_status_is_not_enqueued(
    monkeypatch: pytest.MonkeyPatch, status: str | None
) -> None:
    rows = [_project_row("p-terminal", status)]
    enqueued = asyncio.run(_run_cron_over(monkeypatch, rows))
    assert enqueued == [], f"{status!r} should be terminal (not enqueued)"


@pytest.mark.parametrize("status", ACTIVE_STATUSES)
def test_active_status_is_enqueued(
    monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    rows = [_project_row("p-active", status)]
    enqueued = asyncio.run(_run_cron_over(monkeypatch, rows))
    assert enqueued == ["p-active"], f"{status!r} should be enqueued"


def test_oppstart_fakturert_specifically_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Direct regression guard for gotchas.md §19: a 50%-billed project sitting
    at `Oppstart fakturert` must NOT be re-enqueued by the hourly poller."""
    rows = [_project_row("p-50", FAKTURA_STATUS_50)]
    enqueued = asyncio.run(_run_cron_over(monkeypatch, rows))
    assert enqueued == []


def test_mixed_batch_only_enqueues_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A realistic batch: only the operator-intent rows come through."""
    rows = [
        _project_row("p1", FAKTURA_STATUS_50),  # skip
        _project_row("p2", FAKTURA_STATUS_TIL_AVSLUTNING),  # enqueue
        _project_row("p3", FAKTURA_STATUS_FULL),  # skip
        _project_row("p4", FAKTURA_STATUS_TIL_OPPSTART),  # enqueue
        _project_row("p5", None),  # skip
        _project_row("p6", FAKTURA_STATUS_KREDITERT),  # skip
    ]
    enqueued = asyncio.run(_run_cron_over(monkeypatch, rows))
    assert enqueued == ["p2", "p4"]
