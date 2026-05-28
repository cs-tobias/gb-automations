"""Recompute a Leveranse's status from its Korreksjon rollup (Phase 2.5).

Enqueued by either propagation engine after a Korreksjon row's Ferdig
state changes:
  - sync_frame_comments propagating a Frame `comment.completed` event
    to Notion → recheck rollup.
  - sync_oppgave_done propagating a Notion checkbox toggle to Frame →
    recheck rollup.

Algorithm:
  1. Read current status. If in MANUAL_DELIVERABLE_STATUSES (Trenger
     avklaring / Utgår), return "skipped_manual" — the team's override
     wins. (set_deliverable_status also gates on this, but the early
     exit saves us the Oppgaver query.)
  2. Find the ACTIVE Korreksjonsrunde row: the one with the highest
     `Runde` number among Korreksjonsrunde-kind rows related to this
     Leveranse. (Older rounds stay in Notion as history; we only roll
     up the current round.) None → return "skipped_no_round".
  3. Count its Korreksjon children: total + done_count via
     `notion_client.count_korreksjon_children`. Three cases:
       - done == 0:               leave status alone (don't downgrade from
                                  Klar til oppstart).
       - 0 < done < total:        Under arbeid.
       - done == total > 0:       Oppgaver ferdig. Also auto-tick the
                                  round row's own Ferdig checkbox.
  4. Call set_deliverable_status. Read-first / skip-if-same handles the
     "no actual change" case.

Idempotent — the rollup is a pure function of the current Oppgaver
state. Multiple consecutive recheck tasks on the same Leveranse
collapse to a single one via the active-dedup index on sync_tasks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    KORREKSJON_KIND_KORREKSJONSRUNDE,
    MANUAL_DELIVERABLE_STATUSES,
    OPPGAVER_PROPS,
    STATUS_OPPGAVER_FERDIG,
    STATUS_UNDER_ARBEID,
    settings,
)

logger = logging.getLogger(__name__)


@dataclass
class LeveranseStatusResult:
    leveranse_page_id: str
    active_round_oppgave_id: str | None = None
    active_round_number: int | None = None
    total: int = 0
    done: int = 0
    action: str = "skipped"  # written | unchanged | skipped_manual | skipped_no_round | skipped_no_children | failed
    note: str | None = None
    new_status: str | None = None


async def _find_active_korreksjonsrunde(
    leveranse_page_id: str,
) -> tuple[str | None, int | None]:
    """Return (page_id, round_number) of the highest-Runde Korreksjonsrunde
    row for this Leveranse, or (None, None) if there are none.

    The "active" round = the latest one the team's working on. Older
    rounds (Korreksjonsrunde 1, 2 when we're on 3) stay in Notion as
    history; we don't roll them up because their children may legitimately
    be incomplete (the team moved on without ticking everything).
    """
    if not settings.oppgaver_db_id:
        return None, None
    body: dict[str, Any] = {
        "filter": {
            "and": [
                {
                    # Korreksjonsrunde rows are sub-items of the deliverable —
                    # the Parent item relation points at it.
                    "property": OPPGAVER_PROPS["parent"],
                    "relation": {"contains": leveranse_page_id},
                },
                {
                    "property": OPPGAVER_PROPS["kind"],
                    "select": {"equals": KORREKSJON_KIND_KORREKSJONSRUNDE},
                },
            ]
        },
        # Sort by Runde descending — first result is the highest round.
        "sorts": [{"property": OPPGAVER_PROPS["round"], "direction": "descending"}],
        "page_size": 5,  # we only need the first; small page for cheap call
    }
    from gb_automations.clients.notion import _client, _raise_for_status, _with_retries

    async with _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/databases/{settings.oppgaver_db_id}/query", json=body
            ),
            op_name="POST /databases/oppgaver/query active_korreksjonsrunde",
        )
        _raise_for_status(response)
        results = response.json().get("results", [])
    for row in results:
        if row.get("archived") or row.get("in_trash"):
            continue
        page_id = row.get("id")
        props = row.get("properties") or {}
        round_prop = props.get(OPPGAVER_PROPS["round"]) or {}
        round_number = round_prop.get("number")
        if isinstance(round_number, (int, float)):
            return page_id, int(round_number)
        # Korreksjonsrunde row without a Runde number is malformed;
        # skip it and keep searching.
    return None, None


async def recheck_leveranse_status(
    leveranse_page_id: str,
) -> LeveranseStatusResult:
    """Drain one leveranse_status_recheck task: recompute status from
    the active Korreksjonsrunde's children, write if different.
    """
    result = LeveranseStatusResult(leveranse_page_id=leveranse_page_id)

    # 1. Manual override gate (early exit before the Oppgaver query).
    try:
        current_status = await notion_client.get_deliverable_status(leveranse_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "leveranse_status: get_deliverable_status failed for %s",
            leveranse_page_id,
        )
        result.action = "failed"
        result.note = f"Notion get_status: {err}"
        return result

    if current_status in MANUAL_DELIVERABLE_STATUSES:
        logger.info(
            "leveranse_status: skipped (current=%r is manual) for %s",
            current_status,
            leveranse_page_id,
        )
        result.action = "skipped_manual"
        result.note = f"manual override: {current_status}"
        return result

    # 2. Find the active round.
    try:
        runde_oppgave_id, round_number = await _find_active_korreksjonsrunde(
            leveranse_page_id
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "leveranse_status: find_active_korreksjonsrunde failed for %s",
            leveranse_page_id,
        )
        result.action = "failed"
        result.note = f"Notion query: {err}"
        return result

    if runde_oppgave_id is None:
        logger.info(
            "leveranse_status: no Korreksjonsrunde rows for %s — skipping",
            leveranse_page_id,
        )
        result.action = "skipped_no_round"
        return result

    result.active_round_oppgave_id = runde_oppgave_id
    result.active_round_number = round_number

    # 3. Count children.
    try:
        total, done = await notion_client.count_korreksjon_children(
            runde_oppgave_id
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "leveranse_status: count_korreksjon_children failed for %s",
            runde_oppgave_id,
        )
        result.action = "failed"
        result.note = f"Notion query: {err}"
        return result

    result.total = total
    result.done = done

    if total == 0:
        # Round row exists but no Korreksjon children yet — typical when
        # the round was just created and we haven't seen the comments
        # land as sub-rows. Leave status alone.
        logger.info(
            "leveranse_status: round %s has 0 Korreksjon children — skipping",
            runde_oppgave_id,
        )
        result.action = "skipped_no_children"
        return result

    if done == 0:
        # Don't downgrade from Klar til oppstart back to anything else
        # just because nothing's checked yet.
        logger.info(
            "leveranse_status: round %s has 0/%d done — leaving status %r as-is",
            runde_oppgave_id,
            total,
            current_status,
        )
        result.action = "skipped_no_children"
        result.note = f"0/{total} done; not downgrading"
        return result

    target = STATUS_OPPGAVER_FERDIG if done == total else STATUS_UNDER_ARBEID
    result.new_status = target

    # 4. Write status.
    try:
        action = await notion_client.set_deliverable_status(
            leveranse_page_id, target
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "leveranse_status: set_deliverable_status failed for %s",
            leveranse_page_id,
        )
        result.action = "failed"
        result.note = f"Notion set_status: {err}"
        return result

    result.action = action  # written | unchanged | skipped_manual

    # When all Korreksjoner of the round are done, auto-tick the round
    # row's own Ferdig checkbox too (it lives in the Oppgaver DB). Visual
    # signal that the round is wrapped up. set_row_done is read-first/
    # idempotent so an already-ticked round is a no-op. We DO NOT auto-untick
    # on a downgrade — the team can manually flip it back if they reopened a
    # round on purpose.
    if done == total and total > 0:
        try:
            await notion_client.set_row_done(runde_oppgave_id, True)
        except Exception:
            logger.exception(
                "leveranse_status: auto-tick round %s Ferdig failed (non-fatal)",
                runde_oppgave_id,
            )

    logger.info(
        "leveranse_status: leveranse %s round %s (%d/%d done) → %s (%s)",
        leveranse_page_id,
        round_number,
        done,
        total,
        target,
        action,
    )
    return result
