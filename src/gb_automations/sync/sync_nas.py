"""Project → NAS folder mirror.

Provision (and rename in place) the project's folder structure on the office
NAS share. Extracted from sync_labels.py so the NAS step lives behind its
own queue task type (`nas_folder_sync`) — one Notion button per system,
independent retry, independent failure surface. The Initialize button fans
out to label_sync + nas_folder_sync + frame_project_sync; the "Sync NAS"
button enqueues just this task.

Idempotent and self-healing, mirroring sync_frame_project:
  - re-runs are a no-op on a folder whose Notion title matches the cached row,
  - a Notion rename moves the folder in place via the cached `current_name`,
  - a folder a user deleted by hand is recreated on the next sync
    (`ensure_project_folders` is mkdir-exist_ok throughout).

Writes a Windows-facing display path to the Projects DB "NAS" URL property
when `nas_projects_display_root` is configured, so the team can click straight
into File Explorer. The writeback is best-effort: a Notion failure logs and
moves on, the next run retries.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from gb_automations.clients import nas as nas_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import ProjectFolder

logger = logging.getLogger(__name__)


@dataclass
class NasSyncResult:
    """Outcome of one project's NAS folder sync. `action` matches the legacy
    nas_action strings from the old bundled flow so log readers see the same
    vocabulary. `display_path` is the Windows-facing string we PATCHed onto
    the Notion row (None when the writeback was skipped or unconfigured).
    """

    project_page_id: str
    action: str = "skipped"  # created | renamed | unchanged | skipped | failed
    target_path: str | None = None
    display_path: str | None = None
    note: str | None = None


# The container-side root of the bind mount. docker-compose.yml binds
# ${NAS_HOST_PATH}:/mnt/nas, so anything under /mnt/nas in the container is
# the same file on Windows under NAS_HOST_PATH. Hardcoded because the bind
# target is fixed in compose; there's no scenario where someone would change
# one side without the other.
_CONTAINER_NAS_MOUNT = PurePosixPath("/mnt/nas")


def _to_display_path(target: Path) -> str | None:
    """Convert the container-visible NAS path to the team-visible Windows path.

    Example: /mnt/nas/Prosjekt/2026/1300_X → X:\\gb-nas-test\\Prosjekt\\2026\\1300_X
    when NAS_HOST_PATH is "X:\\gb-nas-test". The relationship comes from the
    docker-compose bind ${NAS_HOST_PATH}:/mnt/nas — the segment after /mnt/nas
    in the container IS the segment after NAS_HOST_PATH on Windows, so we just
    re-root and switch separators.

    Returns None when NAS_HOST_PATH isn't configured (writeback skipped) or
    when `target` doesn't sit inside the container mount (defensive —
    shouldn't happen, but if it does we'd rather skip than store something
    misleading).
    """
    if not settings.nas_host_path:
        logger.info(
            "nas: NAS_HOST_PATH not set; skipping Notion URL writeback"
        )
        return None
    # Normalize separators before treating the input as POSIX. The worker runs
    # in a Linux container so `target` already serializes with forward slashes
    # in production; on Windows hosts (local tests) `str(Path(...))` uses
    # backslashes, which would make `relative_to` fail.
    target_posix = PurePosixPath(str(target).replace("\\", "/"))
    try:
        rel = target_posix.relative_to(_CONTAINER_NAS_MOUNT)
    except ValueError:
        logger.warning(
            "NAS target %s is not under %s; skipping Notion writeback",
            target,
            _CONTAINER_NAS_MOUNT,
        )
        return None
    display = PureWindowsPath(settings.nas_host_path, *rel.parts)
    return str(display)


async def sync_nas_folder(project_page_id: str) -> NasSyncResult:
    """Reconcile + create this project's folder structure on the NAS.

    The durable queue worker calls this for a `nas_folder_sync` task;
    idempotent, so retries and re-clicks are safe.

    Re-fetches the page so the folder name reflects the title AT PROCESSING
    TIME (a rename between click and processing is picked up). The year
    segment comes from Notion's immutable created_time, so renames only
    change the leaf — consistent with the Gmail label and Frame mirrors.

    Failure semantics: with NAS now in its own queue task, a filesystem
    error becomes a visible queue failure (parked as failed, 🛑 dot)
    rather than the previous best-effort silent log. That's intentional —
    the operator asked for per-system visibility. Mitigated by the
    worker's existing retry/backoff.
    """
    result = NasSyncResult(project_page_id=project_page_id)

    if not (settings.sync_nas_folders and settings.nas_projects_root):
        result.note = "sync_nas_folders=false or NAS_PROJECTS_ROOT unset"
        return result

    try:
        page = await notion_client.get_page(project_page_id)
    except Exception:
        logger.exception(
            "nas: failed to load Notion project page %s", project_page_id
        )
        result.action = "failed"
        result.note = "Notion page fetch failed"
        return result

    title = notion_client.extract_page_title(page)
    if not title:
        logger.info("nas: page %s has no title yet, skipping", project_page_id)
        result.note = "no title yet"
        return result

    created_time = page.get("created_time")

    if not await asyncio.to_thread(nas_client.nas_available):
        logger.warning(
            "nas: share %r not available (unmounted or read-only); "
            "marking task failed so the operator can see it",
            settings.nas_projects_root,
        )
        result.action = "failed"
        result.note = "NAS share unavailable"
        return result

    # Disciplines are derived from the project's tasks (Oppgaver) — single
    # source of truth: tasks own their own discipline; the project is "the
    # set of disciplines its tasks have". A project with no tasks yet (tilbud
    # phase) gets the always-folders only. No tasks DB / no relation / Notion
    # query failure → empty list (provision always-folders, don't fail the
    # task on a transient Notion blip).
    disciplines: list[str] = []
    try:
        task_pages = await notion_client.tasks_for_project(project_page_id)
        for task_page in task_pages:
            label = notion_client.task_discipline(task_page)
            if label:
                disciplines.append(label)
    except Exception:
        logger.exception(
            "nas: failed to query tasks for project %s; "
            "provisioning always-folders only",
            project_page_id,
        )

    try:
        async with SessionLocal() as session:
            row = await session.get(ProjectFolder, project_page_id)

            if row is None:
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders,
                    title,
                    created_time,
                    disciplines,
                )
                outcome = "created"
            elif row.current_name != title:
                target = await asyncio.to_thread(
                    nas_client.rename_project_folder,
                    row.current_name,
                    title,
                    created_time,
                    disciplines,
                )
                outcome = "renamed"
            else:
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders,
                    title,
                    created_time,
                    disciplines,
                )
                outcome = "unchanged"

            await session.merge(
                ProjectFolder(
                    notion_page_id=project_page_id,
                    current_path=str(target),
                    current_name=title,
                )
            )
            await session.commit()
    except Exception:
        logger.exception(
            "nas: folder sync failed for project %r (%s)", title, project_page_id
        )
        result.action = "failed"
        result.note = "filesystem error"
        return result

    result.action = outcome
    result.target_path = str(target)
    logger.info("nas: %s %s", outcome, target)

    # Best-effort Notion writeback: a failure to PATCH the URL shouldn't fail
    # the queue task — the folder exists on disk and the next sync will retry.
    display_path = _to_display_path(target)
    if display_path:
        try:
            await notion_client.set_project_nas_url(project_page_id, display_path)
            result.display_path = display_path
        except Exception:
            logger.exception(
                "nas: failed to patch NAS path on project page %s", project_page_id
            )

    return result
