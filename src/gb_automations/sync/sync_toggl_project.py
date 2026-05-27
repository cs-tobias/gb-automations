"""Notion Project → Toggl Track project mirror.

Notion is the source of truth; this module reconciles the corresponding
Toggl project so the team's timer dropdowns stay in sync with what's
active in Notion. One Notion project ↔ one Toggl project, by id (NOT
by name — Toggl allows duplicate-named projects in a workspace, so the
TogglProject mapping table is the integration's authoritative link).

Idempotent and self-healing, mirroring sync_frame_project:
  - re-runs are no-ops on Toggl's side (rename only when name actually changed),
  - a cached `toggl_project_id` that now 404s on Toggl is evicted and
    re-created on the next sync (admin deleted it in Toggl's UI),
  - a Toggl project pre-created in the UI with the matching name is
    ADOPTED rather than duplicated.

The queue worker calls `sync_toggl_project` for a `toggl_project_sync`
task. The same Notion Initialize button that enqueues label_sync /
nas_folder_sync / frame_project_sync also enqueues this when
SYNC_TOGGL=true.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import notion as notion_client
from gb_automations.clients import toggl as toggl_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import TogglProject
from gb_automations.utils.labels import project_path_parts

logger = logging.getLogger(__name__)


@dataclass
class TogglProjectResult:
    project_page_id: str
    # adopted = a Toggl project with the same name already existed in the
    # workspace and we matched it instead of creating a duplicate.
    action: str = "skipped"  # created | adopted | renamed | unchanged | skipped | failed
    toggl_project_id: str | None = None
    toggl_url: str | None = None
    note: str | None = None


def _is_stale(err: Exception) -> bool:
    return (
        isinstance(err, toggl_client.TogglAPIError)
        and err.is_stale_object()
    )


def _project_view_url(workspace_id: str, project_id: str) -> str:
    """URL of a Toggl project's detail / Reports view.

    Unlike Frame.io, Toggl's API doesn't return a canonical view URL on
    project responses, so we build it ourselves. The shape
    `https://track.toggl.com/projects/{id}` opens the project's detail
    page when the user is signed in (and the workspace switcher in the
    top-left handles multi-workspace cases).
    """
    return f"https://track.toggl.com/projects/{project_id}"


async def _evict_if_stale(
    session: AsyncSession, row: TogglProject
) -> bool:
    """Return True if `row.toggl_project_id` is gone from Toggl and was evicted.

    Mirrors `_evict_if_stale_project` in sync_frame: trust the cache on any
    non-stale error (transient/auth), only treat a clean "gone" as stale.
    Caller falls through to the create/adopt path when this returns True.
    """
    try:
        await toggl_client.get_project(
            row.toggl_workspace_id, row.toggl_project_id
        )
    except toggl_client.TogglAPIError as err:
        if not _is_stale(err):
            return False
        logger.info(
            "toggl project %s for page %s is gone — evicting cache",
            row.toggl_project_id,
            row.notion_page_id,
        )
        await session.delete(row)
        await session.flush()
        return True
    except Exception:
        return False
    return False


async def _find_workspace_project_by_name(
    workspace_id: str, name: str
) -> dict | None:
    """Return the Toggl project object whose `name` matches, or None.

    Parallel to `_find_workspace_project_by_name` in sync_frame — Toggl
    also allows duplicate-named projects, so without this check a project
    pre-created in Toggl's UI (or left behind by a cache wipe) would
    silently get a sibling. Used only at first-create time; once the
    mapping row exists, lookup is by id and never by name.
    """
    projects = await toggl_client.list_projects(workspace_id)
    for project in projects:
        if project.get("name") == name:
            return project
    return None


async def sync_toggl_project(project_page_id: str) -> TogglProjectResult:
    """Mirror one Notion Project page to a Toggl Track project. Idempotent
    + self-healing. Called by the queue worker for a `toggl_project_sync`
    task; safe to re-run on every button click.

    Re-fetches the page so the project name reflects the title AT PROCESSING
    TIME (a rename between click and processing is picked up). Toggl renames
    preserve the project id, so the cached URL stays valid.
    """
    result = TogglProjectResult(project_page_id=project_page_id)

    if not settings.sync_toggl:
        result.note = "SYNC_TOGGL=false"
        return result
    if not settings.toggl_workspace_id:
        result.note = "TOGGL_WORKSPACE_ID not set"
        return result

    try:
        page = await notion_client.get_page(project_page_id)
    except Exception:
        logger.exception(
            "toggl: failed to load Notion project page %s", project_page_id
        )
        result.action = "failed"
        return result

    title = notion_client.extract_page_title(page)
    if not title:
        logger.info("toggl: page %s has no title yet, skipping", project_page_id)
        result.note = "no title yet"
        return result

    # Reuse the same sanitization Gmail/NAS/Frame go through so the Toggl
    # project name is byte-identical across all four systems. The year
    # half of the tuple is ignored because Toggl projects are flat (no
    # year tier) — just like Frame.
    created_time = page.get("created_time")
    _year, leaf = project_path_parts(title, created_time)

    workspace_id = settings.toggl_workspace_id
    action: str
    project_id: str
    url: str

    try:
        async with SessionLocal() as session:
            row = await session.get(TogglProject, project_page_id)

            if row is not None and await _evict_if_stale(session, row):
                row = None  # fall through to the create branch

            if row is None:
                existing = await _find_workspace_project_by_name(
                    workspace_id, leaf
                )
                if existing is not None:
                    project_id = str(existing["id"])
                    action = "adopted"
                else:
                    created = await toggl_client.create_project(
                        workspace_id, leaf
                    )
                    project_id = str(created["id"])
                    action = "created"
                url = _project_view_url(workspace_id, project_id)
                session.add(
                    TogglProject(
                        notion_page_id=project_page_id,
                        toggl_project_id=project_id,
                        toggl_workspace_id=workspace_id,
                        current_name=leaf,
                        toggl_url=url,
                    )
                )
            elif row.current_name != leaf:
                await toggl_client.update_project(
                    row.toggl_workspace_id,
                    row.toggl_project_id,
                    name=leaf,
                )
                project_id = row.toggl_project_id
                url = row.toggl_url
                row.current_name = leaf
                await session.merge(row)
                action = "renamed"
            else:
                project_id = row.toggl_project_id
                url = row.toggl_url
                action = "unchanged"

            await session.commit()
    except Exception:
        logger.exception(
            "toggl: project sync failed for %s", project_page_id
        )
        result.action = "failed"
        return result

    # Best-effort writeback: the project exists in Toggl and the cache row
    # records the URL, so the next sync will retry the Notion patch if this
    # call fails.
    try:
        await notion_client.set_project_toggl_url(project_page_id, url)
    except Exception:
        logger.exception(
            "toggl: failed to patch Toggl URL on project page %s",
            project_page_id,
        )

    result.action = action
    result.toggl_project_id = project_id
    result.toggl_url = url
    logger.info(
        "toggl project sync %s for %r (page %s) → toggl_project=%s",
        action,
        leaf,
        project_page_id,
        project_id,
    )
    return result
