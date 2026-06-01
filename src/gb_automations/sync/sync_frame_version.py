"""Frame.io new-version event → stack audit (Phase 2.5+).

When Frame fires `file.versioned`, `resource.id` is the **version_stack id**
(not a file id). The receiver enqueues `frame_version_sync` with that id;
this engine drains it.

Historically (pre-audit), this engine assumed the latest child = the new
version + flipped the deliverable's status to `Ferdig` unconditionally.
That broke for the "drag a pre-existing file onto V00" workflow: the
dragged file's `created_at` is older than the freshly-created placeholder,
so it sorts at index 0 in the stack and comments on it get misclassified
as "V00 placeholder noise" by `sync_frame_comments`. The Ferdig flip also
hid the fact that those comments still needed action.

Today this is a thin shim over `audit_frame_stack` — the audit:
  - identifies the placeholder by cached file id (not by sort index)
  - backfills any pre-existing comments into Korreksjon rows
  - mirrors each comment's `completed_at` to Notion's Ferdig checkbox
  - enqueues `leveranse_status_recheck` to compute the correct status
    (`Under arbeid` / `Oppgaver ferdig` / `Ferdig` via the clean-upload
    branch when there are no rounds but a real version exists)

The dataclass returned here keeps the same `leveranse_page_id` /
`project_page_id` / `action` / `note` shape the queue worker
(`_process_frame_version_sync` in `jobs/queue_worker.py`) already
recognizes, so no worker change is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from gb_automations.config import settings
from gb_automations.sync.sync_frame_stack_audit import audit_frame_stack

logger = logging.getLogger(__name__)


@dataclass
class FrameVersionResult:
    """Kept for backward compatibility with the queue worker dispatch.

    Mirrors a subset of `FrameStackAuditResult` — the worker only reads
    `project_page_id` (for the Projects-DB dot) and `action` (for the
    success/failure decision)."""

    stack_id: str
    leveranse_page_id: str | None = None
    project_page_id: str | None = None
    round_number: int | None = None
    action: str = "skipped"  # written | unchanged | skipped | failed | skipped_manual
    note: str | None = None


async def sync_frame_version(stack_id: str) -> FrameVersionResult:
    """Drain one frame_version_sync task by delegating to the stack audit."""
    result = FrameVersionResult(stack_id=stack_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result

    audit = await audit_frame_stack(stack_id)
    result.leveranse_page_id = audit.leveranse_page_id
    result.project_page_id = audit.project_page_id
    # `round_number` was previously the index-based latest version. We
    # leave it None — the audit doesn't expose a single "latest round"
    # value (it may have processed multiple rounds) and the queue worker
    # doesn't read it.
    result.action = audit.action
    result.note = audit.note
    return result
