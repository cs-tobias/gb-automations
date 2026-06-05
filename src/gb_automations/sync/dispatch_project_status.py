"""Worker-side dispatcher for a Notion Projects-DB `Status` change.

The /webhooks/notion/project-status receiver ONLY enqueues one
`project_status_dispatch` task and returns 200 to Notion immediately.
This module does all the actual work the worker drains:

  1. GET the Notion project page (re-fetches because Notion's automation
     payload only carries the page id, not the row's properties).
  2. Placeholder-title gate (skip when the row still carries
     `000_Kunde_Prosjekt TEMPLATE` — see CLAUDE.md / docs/misc/notion-setup.md
     Part 6 for the rationale).
  3. Read the current `Status` option (multi_select / select / status —
     all three shapes handled by extract_project_status).
  4. Fan out to the per-engine enqueues:
        - Provisioning lanes (gmail / nas / toggl / frame + per-leveranse
          Frame fan-out), gated by PROJECT_STATUS_AUTO_PROVISION + each
          engine's own env flag.
        - The Frame project active/inactive lane fires on EVERY status
          change (even unmapped ones like Ferdig/Tapt) so terminal states
          inactivate the Frame entity and reopening re-activates it.

The previous shape did this work inline in the webhook and was tripping
Notion's auto-pause heuristic (see u6r7s8t9o0p1 migration docstring). The
total inline path was ~2-5 seconds on prod (Notion API latency + 5
synchronous Postgres connection-acquire+insert cycles + a paginated
oppgaver_for_project call for the deliverable fan-out). Moving it here
makes the webhook a sub-50ms ack.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    DISCIPLINE_KEYS,
    PROJECT_STATUS_AUTO_PROVISION,
    PROJECTS_PLACEHOLDER_TITLES,
    settings,
)
from gb_automations.sync.queue import (
    enqueue_frame_leveranse_sync,
    enqueue_frame_project_status_sync,
    enqueue_frame_project_sync,
    enqueue_label_sync,
    enqueue_nas_folder_sync,
    enqueue_toggl_project_sync,
)

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    project_page_id: str
    title: str | None = None
    status: str | None = None
    engines: list[str] = field(default_factory=list)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    action: str = "ok"  # ok | skipped | failed
    note: str | None = None


async def _enqueue_frame_deliverables_for_project(
    project_page_id: str,
) -> tuple[int, int]:
    """Fan out `frame_leveranse_sync` over every recognized-discipline
    deliverable under the given project. Returns (queued, total).

    Lifted from routes/webhooks.py's `_enqueue_frame_deliverables_for_project`
    so the dispatcher doesn't have to reach into the routes layer. The
    discipline gate (`Type` ∈ DISCIPLINE_KEYS) matches the per-row Sync
    button's filter exactly — internal tasks (Klargjøre modell / blank
    Type / Korreksjonsrunde sub-rows) are skipped so we don't enqueue
    work that sync_frame_leveranse would just no-op.
    """
    leveranser = await notion_client.oppgaver_for_project(project_page_id)
    leveranse_queued = 0
    for row in leveranser:
        discipline = notion_client.task_discipline(row)
        if not DISCIPLINE_KEYS.get((discipline or "").strip().lower()):
            continue
        leveranse_queued += await enqueue_frame_leveranse_sync(row["id"])
    return leveranse_queued, len(leveranser)


async def dispatch_project_status(project_page_id: str) -> DispatchResult:
    """Worker entrypoint for a project_status_dispatch task.

    Reads live Notion state and fans out to the per-engine enqueues. Idempotent:
    every downstream enqueue is dedup'd via its own active-task unique index,
    so re-running a dispatch that already covered the project's engines is a
    safe no-op.
    """
    result = DispatchResult(project_page_id=project_page_id)

    # 1. Re-fetch the page. Notion's automation payload's `properties` is
    # empty (verified by the parallel oppgave-status / oppgave-done
    # automations); read the live Status + title at process time. A
    # transient Notion failure surfaces as "failed" so the queue retries
    # with backoff (the user's status change is still on the row; we just
    # need to read it once successfully).
    try:
        page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "project_status_dispatch: failed to GET Notion page %s",
            project_page_id,
        )
        result.action = "failed"
        result.note = f"Notion get_page: {err}"
        return result

    title = (notion_client.extract_page_title(page) or "").strip()
    result.title = title
    if title in PROJECTS_PLACEHOLDER_TITLES:
        logger.info(
            "project_status_dispatch: page %s title %r is a placeholder — "
            "skipping auto-provision",
            project_page_id,
            title,
        )
        result.action = "skipped"
        result.note = "placeholder title"
        return result

    status = notion_client.extract_project_status(page)
    result.status = status
    engines = PROJECT_STATUS_AUTO_PROVISION.get(status or "") or set()

    # 2. Frame project active/inactive lane — fires on EVERY status change.
    # The engine itself decides active vs inactive based on the live Notion
    # status. Independent of the provisioning fan-out: a project at Ferdig
    # has no `engines` to provision but still needs its Frame entity flipped
    # to inactive; a project being reopened needs the reverse. Skipped
    # silently inside the engine when there is no FrameProjectFolder cache
    # row.
    if settings.sync_frame:
        inserted = await enqueue_frame_project_status_sync(project_page_id)
        result.results["frame_status"] = {
            "action": "queued" if inserted else "already_queued",
        }
    else:
        result.results["frame_status"] = {
            "action": "skipped",
            "reason": "SYNC_FRAME=false",
        }

    # 3. Provisioning fan-out. Cumulative semantics — every engine in the
    # mapped set runs (idempotent, so already-provisioned systems no-op).
    # Per-engine env-flag gates mirror the manual per-system buttons.
    if "gmail" in engines:
        if settings.sync_gmail_labels:
            inserted = await enqueue_label_sync(project_page_id)
            result.results["gmail"] = {
                "action": "queued" if inserted else "already_queued",
            }
        else:
            result.results["gmail"] = {
                "action": "skipped",
                "reason": "SYNC_GMAIL_LABELS=false",
            }

    if "nas" in engines:
        if settings.sync_nas_folders and settings.nas_projects_root:
            inserted = await enqueue_nas_folder_sync(project_page_id)
            result.results["nas"] = {
                "action": "queued" if inserted else "already_queued",
            }
        else:
            result.results["nas"] = {
                "action": "skipped",
                "reason": "NAS sync is disabled or unconfigured",
            }

    if "toggl" in engines:
        if settings.sync_toggl:
            inserted = await enqueue_toggl_project_sync(project_page_id)
            result.results["toggl"] = {
                "action": "queued" if inserted else "already_queued",
            }
        else:
            result.results["toggl"] = {
                "action": "skipped",
                "reason": "SYNC_TOGGL=false",
            }

    if "frame" in engines:
        if settings.sync_frame:
            inserted = await enqueue_frame_project_sync(project_page_id)
            leveranse_queued, leveranser_total = (
                await _enqueue_frame_deliverables_for_project(project_page_id)
            )
            result.results["frame"] = {
                "action": "queued" if inserted else "already_queued",
                "leveranser_queued": leveranse_queued,
                "leveranser_total": leveranser_total,
            }
        else:
            result.results["frame"] = {
                "action": "skipped",
                "reason": "SYNC_FRAME=false",
            }

    result.engines = sorted(engines)
    logger.info(
        "project_status_dispatch: %r → engines=%s on %r (page %s): %s",
        status,
        result.engines,
        title,
        project_page_id,
        result.results,
    )
    return result
