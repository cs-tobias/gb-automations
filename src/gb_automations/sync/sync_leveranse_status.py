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
       - done == total > 0:       Oppgaver ferdig.
  4. Call set_deliverable_status. Read-first / skip-if-same handles the
     "no actual change" case. The deliverable Status reaching
     `Oppgaver ferdig` IS the round-done signal — there's no per-round
     checkbox.

Idempotent — the rollup is a pure function of the current Oppgaver
state. Multiple consecutive recheck tasks on the same Leveranse
collapse to a single one via the active-dedup index on sync_tasks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    KORREKSJON_KIND_KORREKSJONSRUNDE,
    MANUAL_DELIVERABLE_STATUSES,
    OPPGAVER_PROPS,
    STATUS_FERDIG,
    STATUS_KLAR_TIL_OPPSTART,
    STATUS_OPPGAVER_FERDIG,
    STATUS_UNDER_ARBEID,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameLeveranseFolder

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
    # {round_number: status_written} for every round row we updated this
    # pass. Used for log narration; not load-bearing for control flow.
    rounds_updated: dict[int, str] = field(default_factory=dict)


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
            # Korreksjonsrunde rows are sub-items of the deliverable — the
            # Parent item relation points at it. The Type/kind match is done
            # client-side below (the Type column may be multi_select, on which
            # a `select` filter clause is rejected by Notion).
            "property": OPPGAVER_PROPS["parent"],
            "relation": {"contains": leveranse_page_id},
        },
        # Sort by Runde descending — first result is the highest round.
        "sorts": [{"property": OPPGAVER_PROPS["round"], "direction": "descending"}],
        "page_size": 10,
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
        # Client-side kind match (the Type column may be multi_select).
        if notion_client.task_discipline(row) != KORREKSJON_KIND_KORREKSJONSRUNDE:
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


async def _list_korreksjonsrunder(
    leveranse_page_id: str,
) -> list[tuple[str, int]]:
    """Return every Korreksjonsrunde sub-row for this leveranse as a
    list of `(page_id, round_number)` tuples, sorted ascending by round.

    Mirrors `_find_active_korreksjonsrunde` but doesn't early-return on
    the highest round — we now write per-round Status, so the rollup
    engine needs to see ALL rounds. An old round that wrapped at
    `Oppgaver ferdig` legitimately stays there; an old round whose
    Korreksjoner are still partially open gets the bidirectional
    treatment too (e.g. someone unchecking a box on a historical round
    drops its status back down to `Under arbeid`).
    """
    if not settings.oppgaver_db_id:
        return []
    body: dict[str, Any] = {
        "filter": {
            "property": OPPGAVER_PROPS["parent"],
            "relation": {"contains": leveranse_page_id},
        },
        # Ascending so the log narration reads round 1 → 2 → 3.
        "sorts": [{"property": OPPGAVER_PROPS["round"], "direction": "ascending"}],
        "page_size": 100,
    }
    from gb_automations.clients.notion import _client, _raise_for_status, _with_retries

    rounds: list[tuple[str, int]] = []
    start_cursor: str | None = None
    async with _client() as client:
        while True:
            if start_cursor:
                body["start_cursor"] = start_cursor
            response = await _with_retries(
                lambda: client.post(
                    f"/databases/{settings.oppgaver_db_id}/query", json=body
                ),
                op_name="POST /databases/oppgaver/query list_korreksjonsrunder",
            )
            _raise_for_status(response)
            payload = response.json()
            for row in payload.get("results", []):
                if row.get("archived") or row.get("in_trash"):
                    continue
                if notion_client.task_discipline(row) != KORREKSJON_KIND_KORREKSJONSRUNDE:
                    continue
                page_id = row.get("id")
                props = row.get("properties") or {}
                round_prop = props.get(OPPGAVER_PROPS["round"]) or {}
                round_number = round_prop.get("number")
                if page_id and isinstance(round_number, (int, float)):
                    rounds.append((page_id, int(round_number)))
            if not payload.get("has_more"):
                break
            start_cursor = payload.get("next_cursor")
            if not start_cursor:
                break
    rounds.sort(key=lambda x: x[1])
    return rounds


async def _has_real_version(leveranse_page_id: str) -> bool:
    """Does this deliverable have at least one non-placeholder file in its
    Frame version stack?

    Used by the "no Korreksjonsrunde, no comments anywhere" clean-upload
    branch to decide between `Ferdig` (a real version landed; nobody
    commented; done) and leaving the status untouched at
    `Klar til oppstart` (just the V00 placeholder, no real delivery yet).

    Returns False on any Frame error (treat as "can't confirm a real
    version exists, don't promote"). Returns False when SYNC_FRAME is
    off (no Frame side to read).
    """
    if not settings.sync_frame:
        return False
    async with SessionLocal() as session:
        row = await session.get(FrameLeveranseFolder, leveranse_page_id)
    if row is None:
        return False
    placeholder_id = row.frame_placeholder_file_id
    try:
        file_obj = await frame_client.get_file(placeholder_id)
    except Exception:  # noqa: BLE001
        return False
    parent_id = file_obj.get("parent_id")
    if not parent_id:
        return False
    try:
        children = await frame_client.list_version_stack_children(parent_id)
    except frame_client.FrameAPIError as err:
        if getattr(err, "status_code", None) in (404, 422):
            # Parent is a folder, not a stack → placeholder alone, no real
            # version on top.
            return False
        return False
    except Exception:  # noqa: BLE001
        return False
    # Any child id that is NOT our placeholder is a real version.
    return any(c.get("id") != placeholder_id for c in children if c.get("id"))


async def _write_round_statuses(
    leveranse_page_id: str, result: LeveranseStatusResult
) -> None:
    """Walk every Korreksjonsrunde sub-row of this deliverable and write
    its own Status from a rollup of its Korreksjon children.

    Rule (bidirectional — always reflects the current count):
      - total == 0           → leave alone (round was just created with
                               Klar til oppstart, that's still correct).
      - done == 0 < total    → Klar til oppstart.
      - 0 < done < total     → Under arbeid.
      - done == total > 0    → Oppgaver ferdig.

    Touches ALL rounds (not just the active one). An old round whose
    Korreksjoner were never all checked off legitimately walks itself
    through the same rule. `set_oppgave_status` is read-first / skip-if-
    same and respects manual overrides, so this is safe to run on every
    recheck and on rounds the team has manually pinned.

    Best-effort per round — a Notion blip on one round shouldn't break
    the rest of the recheck (or block the deliverable-status writeback
    that follows). Errors get logged and the loop moves on.
    """
    try:
        rounds = await _list_korreksjonsrunder(leveranse_page_id)
    except Exception:
        logger.exception(
            "leveranse_status: _list_korreksjonsrunder failed for %s — "
            "skipping per-round status writes (deliverable status still ran)",
            leveranse_page_id,
        )
        return
    for round_page_id, round_number in rounds:
        try:
            total, done = await notion_client.count_korreksjon_children(round_page_id)
        except Exception:
            logger.exception(
                "leveranse_status: count_korreksjon_children failed for "
                "round %s (round %d) — skipping",
                round_page_id,
                round_number,
            )
            continue
        if total == 0:
            continue  # freshly created, no comments yet
        if done == 0:
            target = STATUS_KLAR_TIL_OPPSTART
        elif done < total:
            target = STATUS_UNDER_ARBEID
        else:  # done == total > 0
            target = STATUS_OPPGAVER_FERDIG
        try:
            action = await notion_client.set_oppgave_status(round_page_id, target)
        except Exception:
            logger.exception(
                "leveranse_status: set_oppgave_status failed for round %s "
                "(round %d, target=%s) — skipping",
                round_page_id,
                round_number,
                target,
            )
            continue
        if action == "written":
            result.rounds_updated[round_number] = target
            logger.info(
                "leveranse_status: round %d (%s) (%d/%d done) → %s",
                round_number,
                round_page_id,
                done,
                total,
                target,
            )


async def recheck_leveranse_status(
    leveranse_page_id: str,
) -> LeveranseStatusResult:
    """Drain one leveranse_status_recheck task: recompute status from
    the active Korreksjonsrunde's children, write if different.

    Also walks every Korreksjonsrunde sub-row and writes ITS own Status
    from a rollup of its children — the round rows used to land with
    Status blank and stay there. `_write_round_statuses` runs FIRST so
    the per-round writes happen regardless of which deliverable branch
    short-circuits (clean-upload, manual override, no rounds, etc.).
    """
    result = LeveranseStatusResult(leveranse_page_id=leveranse_page_id)

    # 0. Per-round Status writeback. Runs unconditionally — it's
    # orthogonal to the deliverable's own status branch, and we want
    # it to fire even when the deliverable is at a manual state
    # (Trenger avklaring / Utgår) that would otherwise return early.
    await _write_round_statuses(leveranse_page_id, result)

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
        # No correction rounds at all. Two sub-cases:
        #
        #   (a) "Clean upload" — a real version was uploaded on top of the
        #       V00 placeholder and nobody commented on it. The team's
        #       expectation here is `Ferdig`: the deliverable is done and
        #       went through without correction. Detect this by reading
        #       the placeholder's version stack; if there's a non-
        #       placeholder sibling, promote to Ferdig.
        #
        #   (b) "Pre-delivery" — the placeholder exists alone, no real
        #       version yet. Leave status alone (it'll be `Klar til
        #       oppstart` from sync_frame_leveranse).
        #
        # Without (a) the post-audit refactor would leave clean uploads
        # stuck at `Klar til oppstart` forever — the audit no longer
        # writes Ferdig blindly on file.versioned; this branch does it
        # for the "no comments anywhere" case.
        promote = await _has_real_version(leveranse_page_id)
        if not promote:
            logger.info(
                "leveranse_status: no Korreksjonsrunde rows and no real "
                "version on stack for %s — skipping",
                leveranse_page_id,
            )
            result.action = "skipped_no_round"
            return result

        try:
            action = await notion_client.set_deliverable_status(
                leveranse_page_id, STATUS_FERDIG
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "leveranse_status: set_deliverable_status(Ferdig) failed for %s "
                "(clean-upload branch)",
                leveranse_page_id,
            )
            result.action = "failed"
            result.note = f"Notion set_status: {err}"
            return result
        result.new_status = STATUS_FERDIG
        result.action = action
        result.note = "clean upload (no comments on real version) → Ferdig"
        logger.info(
            "leveranse_status: leveranse %s — clean upload, no comments → Ferdig (%s)",
            leveranse_page_id,
            action,
        )
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
