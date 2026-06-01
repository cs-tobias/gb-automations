"""Bidirectional Utgår mirror — Notion deliverable Status ↔ Frame.io
custom Status field on the file.

ONE engine, called from BOTH directions:
  - Notion automation on the Oppgaver DB fires `/webhooks/notion/oppgave-status`
    when a deliverable's `Status` select changes → enqueue
    `frame_file_status_sync` keyed on the deliverable page id, with the
    `user_email` slot stashing the source hint `"notion"`.
  - Frame webhook on the file's custom-fields-changed event fires
    `/webhooks/frame` → enqueue the same task type, also keyed on the
    deliverable page id (resolved from the file id via
    `FrameLeveranseFolder.frame_placeholder_file_id`), with the source
    hint `"frame"`.

The engine reads BOTH sides at process time. The source hint resolves
the inherent two-state ambiguity (a state difference can mean "Notion
just changed" or "Frame just changed" — without a "last changed"
timestamp on either side, the only way to tell is to record which
webhook fired). The non-source side gets overwritten to match.

Scope: only the Utgår value is mirrored. Other Notion deliverable
statuses (Klar til oppstart / Under arbeid / Oppgaver ferdig / Ferdig /
Trenger avklaring) DO NOT push to Frame. The mirror is "Utgår presence":
either both sides have it, or neither does.

Loop guard: both `set_oppgave_status` (Notion side) and `set_file_status`
(Frame side) are read-first / skip-if-same. When our own write echoes
back through the OTHER side's webhook, the reconcile engine reads both
sides, sees they already match, and returns `unchanged` — no second
write, no oscillation.

Safe defaults: when `FRAME_STATUS_FIELD_ID` is unset the engine returns
`skipped` without touching either side; same when no `FrameLeveranseFolder`
row exists for the deliverable (manual Notion row, or a row whose Frame
side has never been provisioned).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    FRAME_STATUS_UTGAAR,
    STATUS_UTGAAR,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameLeveranseFolder, FrameProjectFolder

logger = logging.getLogger(__name__)


@dataclass
class FrameFileStatusResult:
    leveranse_page_id: str
    source: str = "notion"  # "notion" or "frame" — which side triggered this run
    project_page_id: str | None = None
    frame_project_id: str | None = None
    frame_file_id: str | None = None
    notion_status: str | None = None
    frame_status: str | None = None
    action: str = "skipped"  # written_frame | written_notion | unchanged | skipped | failed
    note: str | None = None


async def _resolve_status_target_file_id(placeholder_file_id: str) -> str:
    """Return the Frame file id that the deliverable's Status badge
    should be written to.

    Frame stacks the V00 placeholder + V01/V02/... in a single
    `version_stack`; the UI displays the badge on whichever version is
    currently selected (usually the latest). We pick the newest non-
    placeholder child so the team sees the badge on the image they're
    actually reviewing.

    Fallbacks (each best-effort — never raises):
      - Placeholder has no parent_id → return the placeholder.
      - Parent is a folder, not a stack → return the placeholder.
      - Stack contains only the placeholder → return the placeholder.
      - Any Frame API error → return the placeholder.

    The placeholder is the cached anchor in FrameLeveranseFolder, so
    using it as the fallback keeps the engine working even when the
    Frame side doesn't have a real version uploaded yet (a pre-delivery
    Utgår still ends up visible on the only file that exists).
    """
    try:
        file_obj = await frame_client.get_file(placeholder_file_id)
    except Exception:  # noqa: BLE001
        return placeholder_file_id
    parent_id = (
        file_obj.get("parent_id") if isinstance(file_obj, dict) else None
    )
    if not parent_id:
        return placeholder_file_id
    try:
        children = await frame_client.list_version_stack_children(parent_id)
    except frame_client.FrameAPIError as err:
        if getattr(err, "status_code", None) in (404, 422):
            # Parent is a folder, not a stack — bare V00, no real version.
            return placeholder_file_id
        return placeholder_file_id
    except Exception:  # noqa: BLE001
        return placeholder_file_id

    non_placeholder = [
        c for c in children
        if isinstance(c, dict) and c.get("id") and c.get("id") != placeholder_file_id
    ]
    if not non_placeholder:
        return placeholder_file_id
    # Latest = newest by created_at. Same ordering as the audit engine.
    non_placeholder.sort(key=lambda c: c.get("created_at", ""))
    latest = non_placeholder[-1]
    target_id = latest.get("id")
    if not isinstance(target_id, str):
        return placeholder_file_id
    logger.info(
        "frame_file_status: targeting latest version %s in stack %s "
        "(placeholder %s)",
        target_id,
        parent_id,
        placeholder_file_id,
    )
    return target_id


async def sync_frame_file_status(
    leveranse_page_id: str, *, source: str = "notion"
) -> FrameFileStatusResult:
    """Drain one frame_file_status_sync task: reconcile Notion ↔ Frame
    Utgår state for one deliverable.

    `source` resolves the two-state ambiguity: when the two sides
    differ, the side named in `source` is treated as authoritative and
    the other side is overwritten to match.

      source="notion" — Notion side just changed; push to Frame.
      source="frame"  — Frame side just changed; push to Notion.

    Defaults to "notion" (the user's primary use case is setting Utgår
    in Notion). The active-dedup index collapses a near-simultaneous
    Notion + Frame nudge to one task; the first webhook to land sets
    the source for that processing pass.
    """
    result = FrameFileStatusResult(
        leveranse_page_id=leveranse_page_id, source=source
    )

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result
    if not settings.frame_status_field_id:
        result.note = "FRAME_STATUS_FIELD_ID unset"
        return result

    # 1. Resolve the deliverable's Frame anchors (file id + project id).
    # Both come from local cache rows — no Frame round-trip yet.
    async with SessionLocal() as session:
        leveranse_row = await session.get(FrameLeveranseFolder, leveranse_page_id)
        if leveranse_row is None:
            logger.info(
                "frame_file_status: no FrameLeveranseFolder for %s — "
                "skipping (manual row, or Frame side never provisioned)",
                leveranse_page_id,
            )
            result.action = "skipped"
            result.note = "no FrameLeveranseFolder cache row"
            return result
        project_row = await session.get(FrameProjectFolder, leveranse_row.project_page_id)

    result.project_page_id = leveranse_row.project_page_id
    placeholder_file_id = leveranse_row.frame_placeholder_file_id

    if project_row is None:
        logger.info(
            "frame_file_status: no FrameProjectFolder for project %s — "
            "skipping (project side not provisioned)",
            leveranse_row.project_page_id,
        )
        result.action = "skipped"
        result.note = "no FrameProjectFolder cache row"
        return result
    result.frame_project_id = project_row.frame_project_id

    # 2. Pick the target file. The V00 placeholder is what FrameLeveranseFolder
    # caches (it's the stable per-leveranse anchor for comment + version
    # lookups), but the team's eyes are on the latest version on top — that's
    # where the Status badge needs to land. Walk the placeholder's version
    # stack and target the newest non-placeholder child. If the placeholder
    # isn't in a stack yet (no real delivery uploaded), fall back to the
    # placeholder itself — a pre-delivery Utgår still makes the badge visible
    # on what the team can see.
    result.frame_file_id = await _resolve_status_target_file_id(placeholder_file_id)

    # 3. Read both sides.
    try:
        notion_status = await notion_client.get_oppgave_status(leveranse_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_file_status: get_oppgave_status failed for %s",
            leveranse_page_id,
        )
        result.action = "failed"
        result.note = f"Notion get_status: {err}"
        return result
    result.notion_status = notion_status

    try:
        frame_status = await frame_client.get_file_status(
            result.frame_file_id, settings.frame_status_field_id
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_file_status: get_file_status failed for file %s",
            result.frame_file_id,
        )
        result.action = "failed"
        result.note = f"Frame get_status: {err}"
        return result
    result.frame_status = frame_status

    notion_is_utgaar = notion_status == STATUS_UTGAAR
    frame_is_utgaar = frame_status == FRAME_STATUS_UTGAAR

    # 3. Already in sync → noop. Catches our own writes bouncing back
    # through the OTHER side's webhook (e.g. Frame webhook fires after
    # we wrote Frame; reconcile reads both sides and sees they match).
    if notion_is_utgaar == frame_is_utgaar:
        result.action = "unchanged"
        result.note = (
            f"already in sync: notion={notion_status!r} frame={frame_status!r}"
        )
        return result

    # 4. Decide direction from the source hint. The side named in
    # `source` is authoritative; the other side gets written to match.
    if source == "frame":
        # Frame is source of truth → reflect Frame's Utgår-presence
        # into Notion. Use force_set_oppgave_status both directions
        # (setting AND clearing) because Utgår is in
        # MANUAL_DELIVERABLE_STATUSES, and the standard
        # set_oppgave_status would refuse to overwrite it — but we ARE
        # the explicit manual move triggered by an explicit Frame UI
        # action.
        desired_notion = STATUS_UTGAAR if frame_is_utgaar else None
        try:
            action = await notion_client.force_set_oppgave_status(
                leveranse_page_id, desired_notion
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "frame_file_status: force_set_oppgave_status failed for %s",
                leveranse_page_id,
            )
            result.action = "failed"
            result.note = f"Notion set_status: {err}"
            return result
        if action == "unchanged":
            # Already matched after the race — caller's webhook fired
            # but the state already aligns. Loop-guard fallthrough.
            result.action = "unchanged"
            result.note = "Notion already matches Frame (loop guard)"
        else:
            result.action = "written_notion"
            logger.info(
                "frame_file_status: deliverable %s — Frame=%r → Notion=%r",
                leveranse_page_id,
                frame_status,
                desired_notion,
            )
        return result

    # source == "notion" (default): Notion is source of truth → push
    # Notion's Utgår-presence to Frame.
    desired_frame = FRAME_STATUS_UTGAAR if notion_is_utgaar else None
    try:
        action = await frame_client.set_file_status(
            project_id=result.frame_project_id,
            file_id=result.frame_file_id,
            field_id=settings.frame_status_field_id,
            value=desired_frame,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_file_status: set_file_status failed for file %s",
            result.frame_file_id,
        )
        result.action = "failed"
        result.note = f"Frame set_status: {err}"
        return result
    if action == "unchanged":
        result.action = "unchanged"
        result.note = "Frame already matches Notion (loop guard)"
    else:
        result.action = "written_frame"
        logger.info(
            "frame_file_status: deliverable %s — Notion=%r → Frame file %s Status=%r",
            leveranse_page_id,
            notion_status,
            result.frame_file_id,
            desired_frame,
        )
    return result


__all__ = ["FrameFileStatusResult", "sync_frame_file_status"]
