"""Project / Task → Frame.io folder mirror.

Phase 1 of the Frame.io integration. Notion is the source of truth; this
module reconciles the corresponding folder structure inside the shared root
Frame.io Project (`FRAME_ROOT_PROJECT_ID`):

    <root project>/<year>/<project leaf>/<discipline>/<task leaf>/placeholder.png

Same `(year, leaf)` split as the NAS layer (utils.labels.project_path_parts),
so a team member can match a NAS folder to a Frame folder by name without
translating anything. Discipline folders are lazily created from each task's
`Type` select (Interiør/Eksteriør/Animasjon); a project with zero tasks gets
no discipline folders.

Idempotent and self-healing, mirroring sync_thread:
  - re-runs are no-ops on Frame's side (rename only when name actually changed),
  - a cached folder/file id that now 404s on Frame is evicted and re-created
    on the next sync (the `frame.get_folder` 404 is the "stale" signal —
    parallel to `notion_client.page_is_live` for Notion ids in sync_thread).

The queue worker calls `sync_frame_project` for a `frame_project_sync` task
and `sync_frame_task` for `frame_task_sync`. Both fan out from the existing
Notion webhook — the same button click that enqueues a Gmail label_sync also
enqueues a Frame sync when SYNC_FRAME=true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    DISCIPLINE_KEYS,
    FRAME_DISCIPLINE_FOLDER_NAMES,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameProjectFolder, FrameTaskFolder
from gb_automations.utils.labels import project_path_parts

logger = logging.getLogger(__name__)


# Per-worker-pass lookup for "year folder under the root project" and
# "discipline folder under a project folder". Both are tiny, and a project
# resync triggers many task syncs back-to-back; without caching we'd LIST the
# root_folder / project folder children once per task. Reset on every public
# entry point so a long-running worker can pick up changes made outside the
# system (e.g. a year folder renamed in Frame's UI).
_year_folder_cache: dict[str, str] = {}
_discipline_folder_cache: dict[tuple[str, str], str] = {}


PLACEHOLDER_FILE_NAME = "placeholder.png"

# Frame's V4 API returns a canonical `view_url` on every folder/file response,
# so we just persist what Frame gives us instead of building URLs from a template.
# That sidesteps any drift in the web-UI URL shape and means a folder opened from
# Notion always lands where Frame itself would link.

# Cached root_folder_id for the configured FRAME_ROOT_PROJECT_ID. The project
# payload includes `root_folder_id` and that id is immutable for the lifetime
# of the Project, so one lookup per worker process is enough.
_root_folder_id_cache: str | None = None


def _view_url(folder_obj: dict, fallback_folder_id: str) -> str:
    """Prefer Frame's own view_url (returned on every folder/file response) and
    fall back to a hand-built URL if absent. The fallback shape was confirmed
    against a live Frame UI URL; it stays here only as a defensive default if
    a future V4 response shape ever omits the field."""
    url = folder_obj.get("view_url") if isinstance(folder_obj, dict) else None
    if url:
        return url
    return (
        f"https://next.frame.io/project/{settings.frame_root_project_id}"
        f"/view/{fallback_folder_id}"
    )


async def _resolve_root_folder_id() -> str:
    global _root_folder_id_cache
    if _root_folder_id_cache:
        return _root_folder_id_cache
    project = await frame_client.get_project(settings.frame_root_project_id)
    root_id = project.get("root_folder_id") or (
        project.get("root_folder") or {}
    ).get("id")
    if not root_id:
        raise RuntimeError(
            "Could not resolve root_folder_id for Frame project "
            f"{settings.frame_root_project_id}. Verify FRAME_ROOT_PROJECT_ID via "
            "/debug/frame/project."
        )
    _root_folder_id_cache = root_id
    return root_id


@dataclass
class FrameProjectResult:
    project_page_id: str
    action: str = "skipped"  # created | renamed | unchanged | skipped | failed
    frame_folder_id: str | None = None
    frame_url: str | None = None
    note: str | None = None


@dataclass
class FrameTaskResult:
    task_page_id: str
    project_page_id: str | None = None
    action: str = "skipped"  # created | renamed | unchanged | skipped | failed
    frame_folder_id: str | None = None
    frame_placeholder_file_id: str | None = None
    frame_url: str | None = None
    note: str | None = None


def _is_404(err: Exception) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) == 404
    )


async def _evict_if_stale_project(
    session: AsyncSession, row: FrameProjectFolder
) -> bool:
    """Return True if `row.frame_folder_id` is gone from Frame and was evicted.

    Mirrors `_evict_if_stale_email_row` in sync_thread: trust the cache on any
    non-404 error (transient/auth), only treat a clean "gone" as stale. Caller
    falls through to the create path when this returns True.
    """
    try:
        await frame_client.get_folder(row.frame_folder_id)
    except frame_client.FrameAPIError as err:
        if not _is_404(err):
            return False
        logger.info(
            "frame project folder %s for page %s is gone — evicting cache",
            row.frame_folder_id,
            row.notion_page_id,
        )
        await session.delete(row)
        await session.flush()
        return True
    except Exception:
        return False
    return False


async def _evict_if_stale_task(
    session: AsyncSession, row: FrameTaskFolder
) -> bool:
    try:
        await frame_client.get_folder(row.frame_folder_id)
    except frame_client.FrameAPIError as err:
        if not _is_404(err):
            return False
        logger.info(
            "frame task folder %s for page %s is gone — evicting cache",
            row.frame_folder_id,
            row.notion_page_id,
        )
        await session.delete(row)
        await session.flush()
        return True
    except Exception:
        return False
    return False


async def _ensure_year_folder(year: str) -> str:
    """Return the Frame folder id for `<root project>/<year>/`, creating it if
    absent. Cached for the lifetime of one worker pass.

    Implementation: resolve the project's root_folder_id once, then list its
    children to find an existing year folder (cheap dedup), creating a new one
    only if missing. Both operations use the standard `/v4/accounts/{aid}/
    folders/{fid}/...` endpoints — there is NO project-scoped "root folder
    children" endpoint in V4 (we tried; it 404s).
    """
    cached = _year_folder_cache.get(year)
    if cached:
        return cached
    root_folder_id = await _resolve_root_folder_id()
    children = await frame_client.list_folder_children(root_folder_id)
    for child in children:
        if child.get("name") == year and child.get("type") in (None, "folder"):
            folder_id = child["id"]
            _year_folder_cache[year] = folder_id
            return folder_id
    new_folder = await frame_client.create_folder(root_folder_id, year)
    folder_id = new_folder["id"]
    _year_folder_cache[year] = folder_id
    return folder_id


async def _ensure_discipline_folder(
    project_folder_id: str, discipline_key: str
) -> str:
    """Find or create the discipline subfolder under a project folder."""
    cache_key = (project_folder_id, discipline_key)
    cached = _discipline_folder_cache.get(cache_key)
    if cached:
        return cached
    name = FRAME_DISCIPLINE_FOLDER_NAMES[discipline_key]
    children = await frame_client.list_folder_children(project_folder_id)
    for child in children:
        if child.get("name") == name and child.get("type") in (None, "folder"):
            folder_id = child["id"]
            _discipline_folder_cache[cache_key] = folder_id
            return folder_id
    new_folder = await frame_client.create_folder(project_folder_id, name)
    folder_id = new_folder["id"]
    _discipline_folder_cache[cache_key] = folder_id
    return folder_id


def _reset_caches() -> None:
    _year_folder_cache.clear()
    _discipline_folder_cache.clear()


async def sync_frame_project(project_page_id: str) -> FrameProjectResult:
    """Mirror one Notion Project page to a Frame.io folder under the root
    project. Idempotent + self-healing. Called by the queue worker for a
    `frame_project_sync` task; safe to re-run on every button click.
    """
    result = FrameProjectResult(project_page_id=project_page_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result
    if not settings.frame_root_project_id:
        result.note = "FRAME_ROOT_PROJECT_ID not set"
        return result

    try:
        page = await notion_client.get_page(project_page_id)
    except Exception:
        logger.exception(
            "frame: failed to load Notion project page %s", project_page_id
        )
        result.action = "failed"
        return result

    title = notion_client.extract_page_title(page)
    if not title:
        logger.info("frame: page %s has no title yet, skipping", project_page_id)
        result.note = "no title yet"
        return result

    created_time = page.get("created_time")
    year, leaf = project_path_parts(title, created_time)

    try:
        async with SessionLocal() as session:
            row = await session.get(FrameProjectFolder, project_page_id)

            if row is not None and await _evict_if_stale_project(session, row):
                row = None  # fall through to the create branch

            if row is None:
                year_folder_id = await _ensure_year_folder(year)
                new_folder = await frame_client.create_folder(year_folder_id, leaf)
                folder_id = new_folder["id"]
                url = _view_url(new_folder, folder_id)
                session.add(
                    FrameProjectFolder(
                        notion_page_id=project_page_id,
                        frame_folder_id=folder_id,
                        frame_parent_id=year_folder_id,
                        current_name=leaf,
                        current_year=year,
                        frame_url=url,
                    )
                )
                action = "created"
            elif row.current_name != leaf:
                renamed = await frame_client.rename_folder(row.frame_folder_id, leaf)
                folder_id = row.frame_folder_id
                url = _view_url(renamed, folder_id)
                row.current_name = leaf
                row.frame_url = url
                await session.merge(row)
                action = "renamed"
            else:
                folder_id = row.frame_folder_id
                url = row.frame_url
                action = "unchanged"

            await session.commit()
    except Exception:
        logger.exception("frame: project sync failed for %s", project_page_id)
        result.action = "failed"
        return result

    # Best-effort: a Notion writeback failure shouldn't fail the whole task —
    # the folder exists in Frame and the cache row records the URL, so the
    # next run will retry the patch.
    try:
        await notion_client.set_project_frame_url(project_page_id, url)
    except Exception:
        logger.exception(
            "frame: failed to patch Frame URL on project page %s", project_page_id
        )

    result.action = action
    result.frame_folder_id = folder_id
    result.frame_url = url
    logger.info(
        "frame project sync %s for %r (page %s) → %s",
        action,
        leaf,
        project_page_id,
        folder_id,
    )
    return result


async def sync_frame_task(task_page_id: str) -> FrameTaskResult:
    """Mirror one Notion Task page to a Frame.io folder + placeholder file
    under its project's discipline subfolder. Recursively provisions the
    parent project if its cache row is missing, so the worker can drain
    `frame_task_sync` tasks out of order without races.
    """
    result = FrameTaskResult(task_page_id=task_page_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result
    if not settings.frame_root_project_id:
        result.note = "FRAME_ROOT_PROJECT_ID not set"
        return result

    try:
        page = await notion_client.get_page(task_page_id)
    except Exception:
        logger.exception(
            "frame: failed to load Notion task page %s", task_page_id
        )
        result.action = "failed"
        return result

    task_name = notion_client.extract_page_title(page)
    project_page_id = notion_client.task_project_id(page)
    discipline_label = notion_client.task_discipline(page)

    if not task_name:
        result.note = "no title yet"
        return result
    if not project_page_id:
        result.note = "no project relation"
        return result
    if not discipline_label:
        result.note = "no Type (discipline) set"
        return result

    result.project_page_id = project_page_id

    discipline_key = DISCIPLINE_KEYS.get(discipline_label.strip().lower())
    if not discipline_key:
        logger.warning(
            "frame: task %s has unrecognized Type %r; skipping",
            task_page_id,
            discipline_label,
        )
        result.note = f"unknown discipline: {discipline_label}"
        return result

    try:
        # Ensure the parent project exists in Frame first. We can't assume
        # frame_project_sync ran before us — the queue can drain in any order,
        # and a task button click doesn't enqueue its project. Recursing is
        # cheap when the cache row is already present (no Frame API call).
        async with SessionLocal() as session:
            project_row = await session.get(FrameProjectFolder, project_page_id)
        if project_row is None:
            await sync_frame_project(project_page_id)
            async with SessionLocal() as session:
                project_row = await session.get(
                    FrameProjectFolder, project_page_id
                )
        if project_row is None:
            logger.warning(
                "frame: parent project sync for %s yielded no cache row; "
                "task %s cannot be provisioned this round",
                project_page_id,
                task_page_id,
            )
            result.note = "parent project not provisioned"
            return result

        discipline_folder_id = await _ensure_discipline_folder(
            project_row.frame_folder_id, discipline_key
        )

        async with SessionLocal() as session:
            row = await session.get(FrameTaskFolder, task_page_id)

            if row is not None and await _evict_if_stale_task(session, row):
                row = None

            if row is None:
                new_folder = await frame_client.create_folder(
                    discipline_folder_id, task_name
                )
                folder_id = new_folder["id"]
                placeholder = await frame_client.create_file_from_url(
                    folder_id, PLACEHOLDER_FILE_NAME, settings.frame_placeholder_url
                )
                placeholder_id = placeholder["id"]
                url = _view_url(new_folder, folder_id)
                session.add(
                    FrameTaskFolder(
                        notion_page_id=task_page_id,
                        project_page_id=project_page_id,
                        frame_folder_id=folder_id,
                        frame_placeholder_file_id=placeholder_id,
                        current_name=task_name,
                        current_discipline=discipline_label,
                        frame_url=url,
                    )
                )
                action = "created"
            elif row.current_name != task_name:
                # Discipline change is logged but not re-parented in Phase 1 —
                # cross-folder move requires a V4 endpoint we haven't verified
                # safe yet; the folder stays under its original discipline and
                # only the name is updated. Revisit once the Frame UI behavior
                # of stale parented folders is observed.
                if row.current_discipline != discipline_label:
                    logger.warning(
                        "frame: task %s discipline changed %r→%r — renaming "
                        "in place under old discipline; re-parenting is not "
                        "yet implemented",
                        task_page_id,
                        row.current_discipline,
                        discipline_label,
                    )
                renamed = await frame_client.rename_folder(row.frame_folder_id, task_name)
                folder_id = row.frame_folder_id
                placeholder_id = row.frame_placeholder_file_id
                url = _view_url(renamed, folder_id)
                row.current_name = task_name
                row.current_discipline = discipline_label
                row.frame_url = url
                await session.merge(row)
                action = "renamed"
            else:
                folder_id = row.frame_folder_id
                placeholder_id = row.frame_placeholder_file_id
                url = row.frame_url
                action = "unchanged"

            await session.commit()
    except Exception:
        logger.exception("frame: task sync failed for %s", task_page_id)
        result.action = "failed"
        return result

    try:
        await notion_client.set_task_frame_url(task_page_id, url)
    except Exception:
        logger.exception(
            "frame: failed to patch Frame URL on task page %s", task_page_id
        )

    result.action = action
    result.frame_folder_id = folder_id
    result.frame_placeholder_file_id = placeholder_id
    result.frame_url = url
    logger.info(
        "frame task sync %s for %r (page %s, discipline %s) → %s",
        action,
        task_name,
        task_page_id,
        discipline_label,
        folder_id,
    )
    return result


