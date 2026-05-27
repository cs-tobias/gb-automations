"""Frame.io new-version event → Leveranse status "Ferdig" (Phase 2.5).

When the team drags a new image on top of an existing file in Frame, Frame
fires `file.versioned` with `resource.id = <version_stack_id>` (NOT the new
file id — that's a different event, `file.created`). The receiver enqueues
`frame_version_sync` with the stack id; this engine drains it.

Algorithm:
  1. List the stack's children sorted by created_at (oldest = V00, newest = V0N).
  2. Find one whose id matches a cached `FrameLeveranseFolder.frame_placeholder_file_id`.
     That cached file is V00; any newer sibling in the same stack is a real
     delivery → we own this stack.
  3. The latest child is the version that triggered the event; its index in
     the sorted list is the round number (V00=0, V01=1, V02=2, …).
  4. If the latest child is V00 (round 0), the stack only contains the
     placeholder — no real delivery yet, drop silently. (file.versioned
     shouldn't fire for V00 in practice; defense-in-depth.)
  5. Call `set_leveranse_status(Ferdig)`. The helper respects manual
     overrides (Trenger avklaring / Utgår) and skips if already Ferdig.

Self-heal mirrors `sync_frame_comments`:
  - 422/404 on the stack endpoint → not a stack (or deleted), skip.
  - No cached placeholder among stack children → untracked file, skip.

Idempotency: re-running the same stack id is a no-op. The status helper
reads-first and only writes on transition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import STATUS_FERDIG, settings
from gb_automations.db import SessionLocal
from gb_automations.models import FrameLeveranseFolder

logger = logging.getLogger(__name__)


@dataclass
class FrameVersionResult:
    stack_id: str
    leveranse_page_id: str | None = None
    project_page_id: str | None = None
    round_number: int | None = None
    action: str = "skipped"  # written | unchanged | skipped | failed | skipped_manual
    note: str | None = None


def _is_404_or_422(err: Exception) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) in (404, 422)
    )


async def sync_frame_version(stack_id: str) -> FrameVersionResult:
    """Drain one frame_version_sync task: a new version landed on a
    tracked Leveranse's version stack → flip its status to Ferdig.
    """
    result = FrameVersionResult(stack_id=stack_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result

    # 1. List the stack's children. 422/404 → stack id doesn't resolve as
    # a stack (or was deleted between webhook fire and our processing).
    # Skip silently; we'll see the file.created event on a future upload
    # if anyone reuploads.
    try:
        children = await frame_client.list_version_stack_children(stack_id)
    except frame_client.FrameAPIError as err:
        if _is_404_or_422(err):
            logger.info(
                "frame_version: stack %s 4xx — not a stack or deleted; skipping",
                stack_id,
            )
            result.action = "skipped"
            result.note = "stack 4xx"
            return result
        logger.exception("frame_version: list_version_stack_children failed for %s", stack_id)
        result.action = "failed"
        result.note = f"Frame: {err}"
        return result
    except Exception as err:  # noqa: BLE001
        logger.exception("frame_version: list children crashed for %s", stack_id)
        result.action = "failed"
        result.note = str(err)
        return result

    if not children:
        logger.info("frame_version: stack %s has no children — skipping", stack_id)
        result.action = "skipped"
        result.note = "stack empty"
        return result

    # 2. Sort by created_at — Frame's UI orders V00, V01, V02 by upload
    # time, and created_at backs that ordering. Latest child = the new
    # version that triggered the event.
    children.sort(key=lambda c: c.get("created_at", ""))
    child_ids = [c.get("id") for c in children if c.get("id")]

    # 3. Match against our cache. The placeholder we created at
    # Initialize time has a stable file id; finding it among the stack
    # children proves this stack belongs to a Leveranse we manage.
    async with SessionLocal() as session:
        stmt = select(FrameLeveranseFolder).where(
            FrameLeveranseFolder.frame_placeholder_file_id.in_(child_ids)
        )
        leveranse_row = (await session.execute(stmt)).scalar_one_or_none()

    if leveranse_row is None:
        logger.info(
            "frame_version: stack %s has %d children but none match a cached "
            "placeholder — untracked stack, skipping",
            stack_id,
            len(children),
        )
        result.action = "skipped"
        result.note = "untracked stack"
        return result

    leveranse_page_id = leveranse_row.notion_page_id
    project_page_id = leveranse_row.project_page_id
    result.leveranse_page_id = leveranse_page_id
    result.project_page_id = project_page_id

    # 4. The triggering version is the last child. Its index in the
    # sorted list IS the round number (V00=0, V01=1, …).
    latest = children[-1]
    round_number = len(children) - 1
    result.round_number = round_number

    if round_number == 0:
        # Only V00 in the stack — no real delivery yet. file.versioned
        # shouldn't fire in this case (it fires when a NEW version is
        # added on top), but defense-in-depth: don't flip status.
        logger.info(
            "frame_version: stack %s has only V00 (file %s) — no real delivery, skipping",
            stack_id,
            latest.get("id"),
        )
        result.action = "skipped"
        result.note = "only V00 in stack"
        return result

    # 5. Flip status to Ferdig.
    try:
        action = await notion_client.set_leveranse_status(
            leveranse_page_id, STATUS_FERDIG
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_version: set_leveranse_status failed for %s — queue will retry",
            leveranse_page_id,
        )
        result.action = "failed"
        result.note = f"Notion: {err}"
        return result

    result.action = action  # written | unchanged | skipped_manual
    logger.info(
        "frame_version: stack %s round %d (file %s) → leveranse %s status=Ferdig (%s)",
        stack_id,
        round_number,
        latest.get("id"),
        leveranse_page_id,
        action,
    )
    return result
