"""Task (Oppgaver) → NAS folder sync.

A "task" is a row in Notion's Oppgaver DB, linked to one project and tagged
with one discipline (Interiør/Eksteriør/Animasjon). When the user clicks the
"Sync to Gmail" button on a task row, this handler is queued as a
`task_folder_sync` SyncTask; the worker calls `sync_task_folder` here.

Result: a folder named after the task is created under each of the project's
5 discipline-conditional NAS parents matching the task's discipline (the same
parents `nas._DISCIPLINE_PARENTS` exposes for the project structure). Renames
and discipline changes move folders in place; we never delete on the live NAS.
Idempotent.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from gb_automations.clients import nas as nas_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import TaskFolder

logger = logging.getLogger(__name__)


@dataclass
class TaskFolderResult:
    """Outcome of one task-folder sync. project_page_id lets the worker mirror
    status onto the parent project's row; `action` is the human summary line."""

    task_page_id: str
    project_page_id: str | None = None
    action: str = "skipped"  # created | renamed | unchanged | skipped | failed
    note: str | None = None


async def sync_task_folder(task_page_id: str) -> TaskFolderResult:
    """Provision NAS folders for one Oppgaver task page. Idempotent.

    Reads the task's name, project relation, and Type from Notion; resolves
    the project's name+created_time from the project page; then creates (or
    renames) the per-discipline task folders via the NAS client. The local
    TaskFolder row tracks current name+discipline so we can detect a rename or
    discipline change and `os.rename` in place instead of orphaning.
    """
    result = TaskFolderResult(task_page_id=task_page_id)

    if not (settings.sync_nas_folders and settings.nas_projects_root):
        result.note = "SYNC_NAS_FOLDERS disabled"
        return result

    if not await asyncio.to_thread(nas_client.nas_available):
        logger.warning(
            "↳ NAS unavailable (root %r not a writable dir); skipping task-folder sync",
            settings.nas_projects_root,
        )
        result.action = "failed"
        result.note = "NAS unavailable"
        return result

    page = await notion_client.get_page(task_page_id)
    task_name = notion_client.extract_page_title(page) or ""
    project_page_id = notion_client.task_project_id(page)
    discipline = notion_client.task_discipline(page)

    if not task_name:
        logger.info("↳ task %s has no title yet — skipping", task_page_id)
        result.note = "no title"
        return result
    if not project_page_id:
        logger.info("↳ task %r has no Prosjekt relation — skipping", task_name)
        result.note = "no project"
        return result
    if not discipline:
        logger.info("↳ task %r has no Type — skipping", task_name)
        result.note = "no discipline"
        return result
    result.project_page_id = project_page_id

    project_page = await notion_client.get_page(project_page_id)
    project_title = notion_client.extract_page_title(project_page)
    project_created_time = project_page.get("created_time")
    if not project_title:
        logger.info(
            "↳ task %r: project page %s has no title — skipping",
            task_name,
            project_page_id,
        )
        result.note = "project missing title"
        return result

    try:
        async with SessionLocal() as session:
            row = await session.get(TaskFolder, task_page_id)

            if row is None:
                await asyncio.to_thread(
                    nas_client.ensure_task_folders,
                    project_title,
                    project_created_time,
                    task_name,
                    discipline,
                )
                result.action = "created"
            elif row.current_name == task_name and row.current_discipline == discipline:
                # Heal a hand-deleted folder, no-op otherwise.
                await asyncio.to_thread(
                    nas_client.ensure_task_folders,
                    project_title,
                    project_created_time,
                    task_name,
                    discipline,
                )
                result.action = "unchanged"
            else:
                await asyncio.to_thread(
                    nas_client.rename_task_folders,
                    project_title,
                    project_created_time,
                    row.current_name,
                    task_name,
                    row.current_discipline,
                    discipline,
                )
                result.action = "renamed"

            await session.merge(
                TaskFolder(
                    notion_page_id=task_page_id,
                    project_page_id=project_page_id,
                    current_name=task_name,
                    current_discipline=discipline,
                )
            )
            await session.commit()
        logger.info(
            "↳ task folder %s: %r (discipline=%s) under %r",
            result.action,
            task_name,
            discipline,
            project_title,
        )
    except Exception:
        logger.exception(
            "↳ task folder sync failed for task %r (%s)", task_name, task_page_id
        )
        result.action = "failed"
        result.note = "exception (see traceback)"

    return result
