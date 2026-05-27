"""Notion Ferdig checkbox → Frame.io `completed` state (Phase 2.5).

The Notion automation fires `POST /webhooks/notion/oppgave-done` when the
team toggles the `Ferdig` checkbox on an Oppgaver row. The receiver
enqueues `oppgave_done_sync` with the page id; this engine drains it.

Algorithm:
  1. GET the Oppgave page; read current `Ferdig` checkbox + `Type`.
  2. Skip non-Korreksjon rows (Oppstart / Korreksjonsrunde don't map to
     a Frame comment). Korreksjonsrunde rows DO get Ferdig auto-ticked
     by the rollup engine when all children are done, but that write
     comes from sync_leveranse_status — not via this path.
  3. Find the linked FrameComment by oppgave_page_id. None → row was
     manually created, or a legacy Phase 2 bullet — log + skip.
  4. GET the Frame comment's current `completed_at`. If it already
     matches Notion's checkbox state, skip (LOOP GUARD: we're seeing
     our own write bouncing back via Frame's webhook → our engine →
     Notion's automation → this receiver).
  5. `frame.set_comment_completed(comment_id, checkbox)`.
  6. Enqueue `leveranse_status_recheck` so the rollup runs.

Idempotent: re-running the same Oppgave page id with the same checkbox
state is a no-op via the loop guard.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import OPPGAVE_KIND_KORREKSJON, OPPGAVER_PROPS, settings
from gb_automations.db import SessionLocal
from gb_automations.models import FrameComment

logger = logging.getLogger(__name__)


@dataclass
class OppgaveDoneResult:
    oppgave_page_id: str
    leveranse_page_id: str | None = None
    project_page_id: str | None = None
    frame_comment_id: str | None = None
    desired_done: bool | None = None
    action: str = "skipped"  # written | unchanged | skipped | skipped_loop | failed
    note: str | None = None


def _is_404(err: Exception) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) == 404
    )


async def sync_oppgave_done(oppgave_page_id: str) -> OppgaveDoneResult:
    """Drain one oppgave_done_sync task: propagate the Notion Ferdig
    checkbox state to the linked Frame comment's `completed` field.
    """
    result = OppgaveDoneResult(oppgave_page_id=oppgave_page_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result

    # 1. Read the Oppgave's current Ferdig + Type.
    try:
        page = await notion_client.get_page(oppgave_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "oppgave_done: failed to GET Oppgave page %s",
            oppgave_page_id,
        )
        result.action = "failed"
        result.note = f"Notion get_page: {err}"
        return result

    props = page.get("properties") or {}
    kind_prop = props.get(OPPGAVER_PROPS["kind"]) or {}
    kind = (kind_prop.get("select") or {}).get("name")
    done_prop = props.get(OPPGAVER_PROPS["done"]) or {}
    desired_done = bool(done_prop.get("checkbox"))
    result.desired_done = desired_done

    # 2. Only Korreksjon rows (per-comment) participate in this sync.
    # Oppstart rows have no linked Frame comment. Korreksjonsrunde rows
    # are managed by the rollup engine, not here.
    if kind != OPPGAVE_KIND_KORREKSJON:
        logger.info(
            "oppgave_done: page %s has kind=%r (not Korreksjon) — skipping",
            oppgave_page_id,
            kind,
        )
        result.action = "skipped"
        result.note = f"kind={kind!r}, not Korreksjon"
        return result

    # 3. Find the linked Frame comment.
    async with SessionLocal() as session:
        stmt = select(FrameComment).where(
            FrameComment.oppgave_page_id == oppgave_page_id
        )
        comment_row = (await session.execute(stmt)).scalar_one_or_none()

    if comment_row is None:
        logger.info(
            "oppgave_done: no FrameComment cache row for Oppgave %s — "
            "skipping (manual row or legacy bullet)",
            oppgave_page_id,
        )
        result.action = "skipped"
        result.note = "no linked Frame comment"
        return result

    comment_id = comment_row.frame_comment_id
    result.frame_comment_id = comment_id
    result.leveranse_page_id = comment_row.leveranse_page_id

    # 4. Read Frame's current state. If it already matches Notion's
    # checkbox, we're seeing our own write bouncing back through the
    # webhook cycle → no-op.
    try:
        comment = await frame_client.get_comment(comment_id)
    except frame_client.FrameAPIError as err:
        if _is_404(err):
            logger.info(
                "oppgave_done: Frame comment %s 404 — deleted in Frame; skipping",
                comment_id,
            )
            result.action = "skipped"
            result.note = "Frame comment 404"
            return result
        logger.exception(
            "oppgave_done: frame.get_comment failed for %s",
            comment_id,
        )
        result.action = "failed"
        result.note = f"Frame get_comment: {err}"
        return result

    current_completed = comment.get("completed_at") is not None
    if current_completed == desired_done:
        logger.info(
            "oppgave_done: Frame comment %s already completed=%s — loop guard, skipping",
            comment_id,
            desired_done,
        )
        result.action = "skipped_loop"
        result.note = "Frame already matches Notion (loop guard)"
        # Still enqueue rollup recheck — even though we didn't write
        # to Frame, the Notion side may have just changed state from a
        # different trigger and the Leveranse status may need to update.
        await _enqueue_recheck(comment_row.leveranse_page_id)
        return result

    # 5. PATCH Frame to match Notion.
    try:
        await frame_client.set_comment_completed(comment_id, desired_done)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "oppgave_done: frame.set_comment_completed failed for %s — queue will retry",
            comment_id,
        )
        result.action = "failed"
        result.note = f"Frame set_completed: {err}"
        return result

    result.action = "written"
    logger.info(
        "oppgave_done: Oppgave %s → Frame comment %s completed=%s",
        oppgave_page_id,
        comment_id,
        desired_done,
    )

    # 6. Enqueue rollup recheck.
    await _enqueue_recheck(comment_row.leveranse_page_id)
    return result


async def _enqueue_recheck(leveranse_page_id: str) -> None:
    """Best-effort enqueue. Lazy import to avoid a circular dep:
    queue.py imports the engine modules for typing in some setups."""
    if not leveranse_page_id:
        return
    from gb_automations.sync.queue import enqueue_leveranse_status_recheck

    try:
        await enqueue_leveranse_status_recheck(leveranse_page_id)
    except Exception:
        logger.exception(
            "oppgave_done: enqueue_leveranse_status_recheck failed for %s "
            "(non-fatal)",
            leveranse_page_id,
        )
