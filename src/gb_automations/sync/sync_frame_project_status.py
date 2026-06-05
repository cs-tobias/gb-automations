"""Notion Project Status → Frame.io project active/inactive mirror.

The `/webhooks/notion/project-status` automation fires whenever the team
changes a project's `Status` select in Notion. Most status values either
provision systems (Tilbudsfase / Tilbud godkjent / I produksjon — handled by
PROJECT_STATUS_AUTO_PROVISION) or are no-ops (Klar til oppstart / Venter på
avklaring / Lang pause). Two terminal states — **Ferdig** and **Tapt** —
additionally flip the project's Frame.io entity to `status="inactive"` via
V4 `PATCH /v4/accounts/{aid}/projects/{pid}`. Any OTHER status flips it back
to `active` — so reopening a finished project automatically un-inactivates
the Frame entity.

This engine is *separate* from sync_frame_project (which provisions the
folder tree + V00 placeholders): provisioning and lifecycle have different
dedup keys, different retry semantics, and one can run without the other.
A status flip on a project whose Frame entity is still being provisioned
will simply find no FrameProjectFolder row and skip — the next status edit
(or the user re-clicking the Sync Frame button) does the catch-up.

Algorithm:
  1. GET the Notion project page; read its current `Status` option name
     (via notion_client.extract_project_status — handles both multi_select
     and single-select shapes).
  2. Decide desired Frame status:
       - in PROJECT_STATUS_INACTIVE_TRIGGERS → "inactive"
       - empty / anything else                → "active"
  3. Look up the FrameProjectFolder cache by notion_page_id. None →
     project not provisioned in Frame yet; skip.
  4. GET the Frame project; if its current `status` already matches the
     desired value, skip (LOOP GUARD + idempotency).
  5. `frame.set_project_status(frame_project_id, desired)`. Notion-only:
     we do NOT subscribe to Frame's side of the flip, so there's no risk
     of a self-fired bounce, but the read-first skip is still useful for
     re-runs and double-fires.

Notion-only direction: we never read Frame and write back to Notion's
project Status — Notion is the source of truth for project lifecycle
(see CLAUDE.md).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    PROJECT_STATUS_INACTIVE_TRIGGERS,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameProjectFolder

logger = logging.getLogger(__name__)


@dataclass
class FrameProjectStatusResult:
    project_page_id: str
    notion_status: str | None = None
    desired_frame_status: str | None = None
    frame_project_id: str | None = None
    current_frame_status: str | None = None
    action: str = "skipped"  # written | unchanged | skipped | failed
    note: str | None = None


def _is_404(err: Exception) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) == 404
    )


def _desired_frame_status(notion_status: str | None) -> str:
    """Map Notion's `Status` option to Frame's `active`/`inactive` flag.

    Terminal states (Ferdig, Tapt) → inactive. Everything else, including
    an empty / unset Status, → active. This is the inverse of "skip when
    not mapped" — the auto-provision dispatch can skip unmapped statuses,
    but the inactivate engine fires on EVERY status change to keep Frame
    in sync (a project being reopened by clearing Status, for example,
    should re-activate the Frame entity).
    """
    if notion_status and notion_status in PROJECT_STATUS_INACTIVE_TRIGGERS:
        return "inactive"
    return "active"


async def sync_frame_project_status(
    project_page_id: str,
) -> FrameProjectStatusResult:
    """Drain one `frame_project_status_sync` task: propagate the project's
    current Notion Status to its linked Frame project's `status` field.
    """
    result = FrameProjectStatusResult(project_page_id=project_page_id)

    if not settings.sync_frame:
        # Symmetric with sync_oppgave_done: the env gate makes a
        # disabled-Frame deploy a clean skip rather than a crash on the
        # first Frame API call.
        result.note = "SYNC_FRAME=false"
        return result

    # 1. Read Notion's current Status.
    try:
        page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_project_status: failed to GET Notion page %s",
            project_page_id,
        )
        result.action = "failed"
        result.note = f"Notion get_page: {err}"
        return result

    notion_status = notion_client.extract_project_status(page)
    result.notion_status = notion_status
    desired = _desired_frame_status(notion_status)
    result.desired_frame_status = desired

    # 2. Look up the Frame project id from the local cache. No row =
    # project not yet provisioned in Frame; nothing to do. The next
    # provisioning run (Sync Frame button, or I produksjon auto-fan-out)
    # creates the Frame entity in its current Notion-derived state, so
    # no catch-up is needed here.
    async with SessionLocal() as session:
        stmt = select(FrameProjectFolder).where(
            FrameProjectFolder.notion_page_id == project_page_id
        )
        row = (await session.execute(stmt)).scalar_one_or_none()

    if row is None:
        logger.info(
            "frame_project_status: no FrameProjectFolder for %s — "
            "project not yet provisioned in Frame; skipping",
            project_page_id,
        )
        result.action = "skipped"
        result.note = "no FrameProjectFolder cache row"
        return result

    result.frame_project_id = row.frame_project_id

    # 3. Read Frame's current status. Skip if already at the desired
    # value (loop guard + idempotency — the engine may be invoked twice
    # for the same change, and re-running on an already-correct state
    # should be a clean no-op, not a redundant PATCH).
    try:
        project = await frame_client.get_project(row.frame_project_id)
    except frame_client.FrameAPIError as err:
        if _is_404(err):
            logger.warning(
                "frame_project_status: Frame project %s 404 — deleted in Frame; "
                "skipping (next Sync Frame button click will re-provision)",
                row.frame_project_id,
            )
            result.action = "skipped"
            result.note = "Frame project 404 (deleted)"
            return result
        logger.exception(
            "frame_project_status: frame.get_project failed for %s",
            row.frame_project_id,
        )
        result.action = "failed"
        result.note = f"Frame get_project: {err}"
        return result
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_project_status: frame.get_project crashed for %s",
            row.frame_project_id,
        )
        result.action = "failed"
        result.note = f"Frame get_project: {err}"
        return result

    current = project.get("status")
    result.current_frame_status = current
    if current == desired:
        logger.info(
            "frame_project_status: Frame project %s already status=%r — "
            "skipping (Notion status=%r)",
            row.frame_project_id,
            current,
            notion_status,
        )
        result.action = "unchanged"
        return result

    # 4. PATCH Frame to match Notion.
    try:
        await frame_client.set_project_status(row.frame_project_id, desired)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_project_status: frame.set_project_status failed for %s — "
            "queue will retry",
            row.frame_project_id,
        )
        result.action = "failed"
        result.note = f"Frame set_project_status: {err}"
        return result

    result.action = "written"
    logger.info(
        "frame_project_status: project %s (Frame %s) %r → %r (Notion status=%r)",
        project_page_id,
        row.frame_project_id,
        current,
        desired,
        notion_status,
    )
    return result
