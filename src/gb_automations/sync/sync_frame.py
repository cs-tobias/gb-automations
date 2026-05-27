"""Project / Leveranse → Frame.io mirror.

Notion is the source of truth; this module reconciles the corresponding
**Frame Project** (one per Notion project, at the workspace level) and the
folder structure inside it:

    <workspace>/<ProjectName> (Frame Project)
                 └── (project's root_folder_id, auto-created by Frame)
                     ├── Interiør/
                     │   ├── <project>_..._<LeveranseA>_V00.png
                     │   └── <project>_..._<LeveranseB>_V00.png
                     └── Eksteriør/
                         └── <project>_..._<LeveranseC>_V00.png

Each Notion Project is its own top-level Frame Project, visible in Frame V4's
"Active Projects" view. Discipline folders are lazily created from each
Leveranse's `Type` select (Interiør/Eksteriør/Animasjon/Annet); a project
with no Leveranser gets no discipline folders. Placeholder files sit
DIRECTLY under the discipline folder — there is no per-leveranse wrapping
folder. The placeholder filename embeds the leveranse name so filenames
remain unique within a shared discipline folder; the filename itself is
the visible label in Frame's UI.

Idempotent and self-healing, mirroring sync_thread:
  - re-runs are no-ops on Frame's side (rename only when name actually changed),
  - a cached project/folder/file id that now 404s on Frame is evicted and
    re-created on the next sync (the `frame.get_*` 404 is the "stale" signal —
    parallel to `notion_client.page_is_live` for Notion ids in sync_thread),
  - a Frame Project pre-created in the UI with the same name as a Notion
    project is ADOPTED rather than duplicated (same for nested folders).

The queue worker calls `sync_frame_project` for a `frame_project_sync` task
and `sync_frame_leveranse` for `frame_leveranse_sync`. Both fan out from the
existing Notion webhook — the same button click that enqueues a Gmail
label_sync also enqueues a Frame sync when SYNC_FRAME=true.

In Phase 2, `sync_frame_leveranse` ALSO auto-creates an "Oppstart" Oppgave
row alongside the Frame folder + placeholder, so every Leveranse has a
place to track pre-delivery work. Korreksjonsrunde rows are created later
by sync_frame_comments when Frame comments arrive.
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
    OPPGAVE_KIND_OPPSTART,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameLeveranseFolder, FrameProjectFolder
from gb_automations.utils.labels import project_path_parts

logger = logging.getLogger(__name__)


# Per-worker-pass lookup for "discipline folder under a project's root folder".
# A project resync triggers many task syncs back-to-back; without caching
# we'd LIST the same root folder's children once per task. Reset on every
# public entry point so a long-running worker picks up changes made outside
# the system (e.g. a discipline folder renamed in Frame's UI).
_discipline_folder_cache: dict[tuple[str, str], str] = {}


def _placeholder_filename(project_name: str, task_name: str) -> str:
    """Build the per-task placeholder filename.

    Shape: `<project>_<studio>_<task>_V00.png` — e.g.
    `1230_Metropolis_Orangeriet_Goldbox.no_Vinkel 1_V00.png`. The placeholder
    is always V00; the team's first real delivery uploads on top as V01 in
    Frame's version stack.

    The studio slot comes from settings.frame_filename_studio (default
    "Goldbox.no"). Renaming the studio in .env affects only NEW placeholders
    — existing uploads keep their filename (Frame's version stack is keyed
    by file slot, not name).
    """
    studio = settings.frame_filename_studio or "Goldbox.no"
    return f"{project_name}_{studio}_{task_name}_V00.png"

# Frame's V4 API returns a canonical `view_url` on every project/folder/file
# response, so we just persist what Frame gives us instead of building URLs
# from a template. That sidesteps any drift in the web-UI URL shape and
# means a link opened from Notion always lands where Frame itself would link.


def _project_view_url(project_obj: dict, project_id: str) -> str:
    """URL of a Frame Project page in the web UI. Prefer the project's own
    `view_url` when present; fall back to the canonical shape.

    Folders inside a project use a different URL shape (see _folder_view_url
    below) — splitting the two helpers keeps each fallback honest."""
    url = project_obj.get("view_url") if isinstance(project_obj, dict) else None
    if url:
        return url
    return f"https://next.frame.io/project/{project_id}"


def _folder_view_url(
    folder_obj: dict, folder_id: str, project_id: str
) -> str:
    """URL of a folder inside a Frame Project. Frame returns `view_url` on
    every folder response in practice, so the fallback rarely fires —
    but if it does, we still need a project_id to scope the `/view/...`
    path under, which the caller passes in.
    """
    url = folder_obj.get("view_url") if isinstance(folder_obj, dict) else None
    if url:
        return url
    return f"https://next.frame.io/project/{project_id}/view/{folder_id}"


def _file_view_url(
    file_obj: dict, file_id: str, project_id: str
) -> str:
    """URL of a file inside a Frame Project. Same shape as `_folder_view_url`
    — Frame's `/view/{id}` route resolves to either a folder or a file in V4.
    Under the flattened Leveranse layout, the placeholder file IS the
    leveranse's anchor in Frame, so this is what we write back to Notion.
    """
    url = file_obj.get("view_url") if isinstance(file_obj, dict) else None
    if url:
        return url
    return f"https://next.frame.io/project/{project_id}/view/{file_id}"


@dataclass
class FrameProjectResult:
    project_page_id: str
    # adopted = a Frame Project with the same name already existed in the
    # workspace and we matched it instead of creating a duplicate.
    action: str = "skipped"  # created | adopted | renamed | unchanged | skipped | failed
    frame_project_id: str | None = None
    frame_folder_id: str | None = None  # the Project's root_folder_id
    frame_url: str | None = None
    note: str | None = None


@dataclass
class FrameLeveranseResult:
    leveranse_page_id: str
    project_page_id: str | None = None
    # adopted = a sibling folder with the same name already existed under
    # the discipline folder and we matched it instead of creating a duplicate
    # (and reused its placeholder.png if one was present).
    action: str = "skipped"  # created | adopted | renamed | unchanged | skipped | failed
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
    """Return True if `row.frame_project_id` is gone from Frame and was evicted.

    Mirrors `_evict_if_stale_email_row` in sync_thread: trust the cache on any
    non-404 error (transient/auth), only treat a clean "gone" as stale. Caller
    falls through to the create path when this returns True.

    Checks the Project endpoint (not the folder) because the Project is the
    load-bearing entity — if it's been archived/deleted in Frame, our entire
    cache row needs to go. A live Project's root_folder is guaranteed to
    exist by Frame's invariants.
    """
    try:
        await frame_client.get_project(row.frame_project_id)
    except frame_client.FrameAPIError as err:
        if not _is_404(err):
            return False
        logger.info(
            "frame project %s for page %s is gone — evicting cache",
            row.frame_project_id,
            row.notion_page_id,
        )
        await session.delete(row)
        await session.flush()
        return True
    except Exception:
        return False
    return False


async def _evict_if_stale_leveranse(
    session: AsyncSession, row: FrameLeveranseFolder
) -> bool:
    """Return True if this leveranse's placeholder file is gone from Frame and
    was evicted. Under the flattened layout, `frame_folder_id` points at the
    SHARED discipline folder (not a per-leveranse folder), so a missing
    discipline folder is not a per-leveranse signal; the placeholder file is
    the leveranse's load-bearing anchor. Mirror of _evict_if_stale_project.
    """
    try:
        await frame_client.get_file(row.frame_placeholder_file_id)
    except frame_client.FrameAPIError as err:
        if not _is_404(err):
            return False
        logger.info(
            "frame leveranse placeholder %s for page %s is gone — evicting cache",
            row.frame_placeholder_file_id,
            row.notion_page_id,
        )
        await session.delete(row)
        await session.flush()
        return True
    except Exception:
        return False
    return False


async def _find_child_folder_by_name(
    parent_folder_id: str, name: str
) -> dict | None:
    """Return the child folder object whose `name` matches, or None.

    Centralizes the match-by-name dance used at every folder level (discipline,
    task). Frame.io allows sibling folders with identical names, so any
    "create folder X under Y" path that doesn't pre-check ends up with
    duplicates whenever a folder by that name already exists — created
    manually in the UI, or left behind by a past cache wipe.

    Type filter `(None, "folder")`: not every V4 response carries `type` on
    every child, but when it does folders use "folder" and files use
    something else.
    """
    children = await frame_client.list_folder_children(parent_folder_id)
    for child in children:
        if child.get("name") == name and child.get("type") in (None, "folder"):
            return child
    return None


async def _find_child_file_by_name(
    parent_folder_id: str, name: str
) -> dict | None:
    """Return the child file object whose `name` matches, or None.

    Mirror of _find_child_folder_by_name for the placeholder.png lookup —
    same list_folder_children call returns folders AND files, we just
    filter the opposite way. We accept any non-folder type because the
    exact V4 file type string isn't pinned anywhere in our wrapper; the
    only sibling of a placeholder.png that would slip past this is another
    folder named "placeholder.png", which is nonsensical.
    """
    children = await frame_client.list_folder_children(parent_folder_id)
    for child in children:
        if child.get("name") == name and child.get("type") not in (None, "folder"):
            return child
    return None


async def _find_workspace_project_by_name(
    workspace_id: str, name: str
) -> dict | None:
    """Return the Frame Project object whose `name` matches, or None.

    Parallel to _find_child_folder_by_name but at the workspace level —
    Frame allows duplicate-name siblings at every tier, including Project
    siblings under a workspace. We use this in sync_frame_project's create
    branch to adopt a pre-existing same-name project (manually created in
    the UI, or left behind after a cache wipe) instead of duplicating it.
    """
    projects = await frame_client.list_projects(
        settings.frame_account_id, workspace_id
    )
    for project in projects:
        if project.get("name") == name:
            return project
    return None


def _root_folder_id_of(project_obj: dict) -> str | None:
    """Extract the project's root_folder_id from a Project response, tolerating
    both shapes V4 has returned in practice (top-level field vs nested under
    `root_folder.id`)."""
    return project_obj.get("root_folder_id") or (
        project_obj.get("root_folder") or {}
    ).get("id")


async def _ensure_discipline_folder(
    project_folder_id: str, discipline_key: str
) -> str:
    """Find or create the discipline subfolder under a Project's root folder."""
    cache_key = (project_folder_id, discipline_key)
    cached = _discipline_folder_cache.get(cache_key)
    if cached:
        return cached
    name = FRAME_DISCIPLINE_FOLDER_NAMES[discipline_key]
    existing = await _find_child_folder_by_name(project_folder_id, name)
    if existing is not None:
        folder_id = existing["id"]
    else:
        new_folder = await frame_client.create_folder(project_folder_id, name)
        folder_id = new_folder["id"]
    _discipline_folder_cache[cache_key] = folder_id
    return folder_id


def _reset_caches() -> None:
    _discipline_folder_cache.clear()


async def sync_frame_project(project_page_id: str) -> FrameProjectResult:
    """Mirror one Notion Project page to a Frame.io Project under the
    configured workspace. Idempotent + self-healing. Called by the queue
    worker for a `frame_project_sync` task; safe to re-run on every button
    click.

    Re-fetches the page so the project name reflects the title AT PROCESSING
    TIME (a rename between click and processing is picked up). Renaming a
    Frame Project preserves both its project_id and its root_folder_id, so
    cached child folders + Notion URLs all stay valid.
    """
    result = FrameProjectResult(project_page_id=project_page_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result
    if not settings.frame_workspace_id:
        result.note = "FRAME_WORKSPACE_ID not set"
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

    # We still go through project_path_parts to apply leaf sanitization
    # (illegal-char replacement) — the year half of the tuple is ignored
    # because the Frame layout no longer has a year tier. Keeping the same
    # sanitization as Gmail/NAS means the project name is byte-identical
    # across all three systems.
    created_time = page.get("created_time")
    _year, leaf = project_path_parts(title, created_time)

    try:
        async with SessionLocal() as session:
            row = await session.get(FrameProjectFolder, project_page_id)

            if row is not None and await _evict_if_stale_project(session, row):
                row = None  # fall through to the create branch

            if row is None:
                # Look for an existing Frame Project in this workspace with
                # the matching name BEFORE creating a new one. Same dedup
                # intent as the folder-level adoption: Frame allows
                # duplicate-name projects, so without this check a project
                # pre-created in the UI (or left over from a cache wipe)
                # would silently get a sibling.
                existing = await _find_workspace_project_by_name(
                    settings.frame_workspace_id, leaf
                )
                if existing is not None:
                    project_id = existing["id"]
                    root_folder_id = _root_folder_id_of(existing)
                    # list_projects may not include view_url or root_folder_id
                    # on every entry; fetch the project explicitly when either
                    # is missing so the cached URL is real and downstream
                    # task-folder creation has the right parent.
                    if not existing.get("view_url") or not root_folder_id:
                        adopted = await frame_client.get_project(project_id)
                        root_folder_id = root_folder_id or _root_folder_id_of(
                            adopted
                        )
                    else:
                        adopted = existing
                    if not root_folder_id:
                        raise RuntimeError(
                            f"adopted frame project {project_id} returned no "
                            "root_folder_id"
                        )
                    url = _project_view_url(adopted, project_id)
                    action = "adopted"
                else:
                    new_project = await frame_client.create_project(
                        settings.frame_workspace_id, leaf
                    )
                    project_id = new_project["id"]
                    root_folder_id = _root_folder_id_of(new_project)
                    if not root_folder_id:
                        # Defensive: every create_project response we've seen
                        # carries root_folder_id, but if Frame ever changes
                        # shape we'd silently break task-folder creation
                        # downstream. Raise loudly here.
                        raise RuntimeError(
                            f"create_project for {leaf!r} returned no "
                            "root_folder_id"
                        )
                    url = _project_view_url(new_project, project_id)
                    action = "created"
                session.add(
                    FrameProjectFolder(
                        notion_page_id=project_page_id,
                        frame_project_id=project_id,
                        frame_folder_id=root_folder_id,
                        current_name=leaf,
                        frame_url=url,
                    )
                )
            elif row.current_name != leaf:
                renamed = await frame_client.rename_project(
                    row.frame_project_id, leaf
                )
                project_id = row.frame_project_id
                url = _project_view_url(renamed, project_id)
                row.current_name = leaf
                row.frame_url = url
                await session.merge(row)
                action = "renamed"
            else:
                project_id = row.frame_project_id
                url = row.frame_url
                action = "unchanged"

            await session.commit()
    except Exception:
        logger.exception("frame: project sync failed for %s", project_page_id)
        result.action = "failed"
        return result

    # Best-effort: a Notion writeback failure shouldn't fail the whole task —
    # the project exists in Frame and the cache row records the URL, so the
    # next run will retry the patch.
    try:
        await notion_client.set_project_frame_url(project_page_id, url)
    except Exception:
        logger.exception(
            "frame: failed to patch Frame URL on project page %s", project_page_id
        )

    result.action = action
    result.frame_project_id = project_id
    result.frame_url = url
    # frame_folder_id (the project's root_folder_id) is informational on the
    # result. Read it back from the cache row for unchanged/renamed paths; in
    # the create/adopt branches the local variable is already set above.
    if action in ("renamed", "unchanged"):
        async with SessionLocal() as session:
            row = await session.get(FrameProjectFolder, project_page_id)
            if row is not None:
                result.frame_folder_id = row.frame_folder_id
    else:
        result.frame_folder_id = root_folder_id
    logger.info(
        "frame project sync %s for %r (page %s) → project=%s",
        action,
        leaf,
        project_page_id,
        project_id,
    )
    return result


async def _ensure_oppstart_oppgave(
    leveranse_page_id: str, leveranse_title: str
) -> None:
    """Best-effort: make sure an "Oppstart" Oppgave row exists for this
    Leveranse. Called from `sync_frame_leveranse` after the Frame folder
    + placeholder are provisioned.

    Idempotent — re-running is a no-op once the row exists. An
    Oppgaver-DB outage (or OPPGAVER_DB_ID unset) is logged and swallowed
    so it can't break the Frame sync: the Leveranse + Frame folder + URL
    write-back are the contract of `sync_frame_leveranse`; Oppstart is a
    convenience the team can also create by hand if this fails.
    """
    if not settings.oppgaver_db_id:
        logger.info(
            "frame: OPPGAVER_DB_ID unset — skipping Oppstart auto-create "
            "for leveranse %s",
            leveranse_page_id,
        )
        return
    try:
        existing = await notion_client.find_oppgave_by_round(
            leveranse_page_id, round_number=None
        )
        if existing:
            return
        await notion_client.create_oppgave_row(
            name="Oppstart",
            leveranse_page_id=leveranse_page_id,
            kind=OPPGAVE_KIND_OPPSTART,
            round_number=None,
        )
        logger.info(
            "frame: created Oppstart Oppgave for leveranse %s (%r)",
            leveranse_page_id,
            leveranse_title,
        )
    except Exception:
        logger.exception(
            "frame: Oppstart auto-create for leveranse %s failed — "
            "Frame folder still provisioned, team can create the row "
            "by hand",
            leveranse_page_id,
        )


async def sync_frame_leveranse(leveranse_page_id: str) -> FrameLeveranseResult:
    """Mirror one Notion Leveranse page to a Frame.io folder + placeholder
    file under its project's discipline subfolder. Recursively provisions
    the parent project if its cache row is missing, so the worker can drain
    `frame_leveranse_sync` tasks out of order without races.

    Also auto-creates an "Oppstart" Oppgave row in the Oppgaver DB so the
    team has somewhere to track pre-delivery work — best-effort, skipped
    silently if OPPGAVER_DB_ID isn't configured.
    """
    result = FrameLeveranseResult(leveranse_page_id=leveranse_page_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result
    if not settings.frame_workspace_id:
        result.note = "FRAME_WORKSPACE_ID not set"
        return result

    try:
        page = await notion_client.get_page(leveranse_page_id)
    except Exception:
        logger.exception(
            "frame: failed to load Notion leveranse page %s", leveranse_page_id
        )
        result.action = "failed"
        return result

    leveranse_name = notion_client.extract_page_title(page)
    project_page_id = notion_client.task_project_id(page)
    discipline_label = notion_client.task_discipline(page)

    if not leveranse_name:
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
            "frame: leveranse %s has unrecognized Type %r; skipping",
            leveranse_page_id,
            discipline_label,
        )
        result.note = f"unknown discipline: {discipline_label}"
        return result

    try:
        # Ensure the parent project exists in Frame first. We can't assume
        # frame_project_sync ran before us — the queue can drain in any order,
        # and a Leveranse button click doesn't enqueue its project. Recursing
        # is cheap when the cache row is already present (no Frame API call).
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
                "leveranse %s cannot be provisioned this round",
                project_page_id,
                leveranse_page_id,
            )
            result.note = "parent project not provisioned"
            return result

        # frame_folder_id on the project row IS the Project's root_folder_id —
        # so this is unchanged from before the refactor (the semantics shifted
        # but the column we pass in is the same).
        discipline_folder_id = await _ensure_discipline_folder(
            project_row.frame_folder_id, discipline_key
        )
        # Carry the project_id so we can scope the fallback view_url for any
        # leveranse folder that comes back without one.
        project_id_for_url = project_row.frame_project_id

        # Per-leveranse placeholder filename, e.g.
        #   "1230_Metropolis_Orangeriet_Goldbox.no_Vinkel 1_V00.png"
        # Computed from the project's CURRENT cached name plus the leveranse
        # title + the configured studio slot. The "_V00" marks this as the
        # placeholder version; the team's first real delivery uploads on
        # top of it as V01 in Frame's version stack.
        #
        # Renames in Notion (project or leveranse) won't auto-rename an
        # existing uploaded placeholder file — Frame's version stack keys
        # on file slot id, not name, so the stale filename is purely
        # cosmetic and the upload-on-top workflow still functions. If we
        # ever want to rename the file too, that's a separate enhancement.
        placeholder_filename = _placeholder_filename(
            project_row.current_name, leveranse_name
        )

        async with SessionLocal() as session:
            row = await session.get(FrameLeveranseFolder, leveranse_page_id)

            if row is not None and await _evict_if_stale_leveranse(session, row):
                row = None

            if row is None:
                # Flattened layout: the placeholder file sits directly under
                # the discipline folder — no per-leveranse wrapping folder.
                # Adopt-or-create the placeholder by filename. Filenames carry
                # the project + leveranse name + studio + V00 suffix, so
                # collisions across leveranses in the same discipline aren't
                # possible by construction.
                existing_placeholder = await _find_child_file_by_name(
                    discipline_folder_id, placeholder_filename
                )
                if existing_placeholder is not None:
                    placeholder_id = existing_placeholder["id"]
                    adopted = (
                        existing_placeholder
                        if existing_placeholder.get("view_url")
                        else await frame_client.get_file(placeholder_id)
                    )
                    url = _file_view_url(adopted, placeholder_id, project_id_for_url)
                    action = "adopted"
                else:
                    placeholder = await frame_client.create_file_from_url(
                        discipline_folder_id,
                        placeholder_filename,
                        settings.frame_placeholder_url,
                    )
                    placeholder_id = placeholder["id"]
                    url = _file_view_url(placeholder, placeholder_id, project_id_for_url)
                    action = "created"
                # `frame_folder_id` now stores the SHARED discipline folder id
                # (multiple leveranses in the same discipline legitimately
                # share this value). The per-leveranse anchor is
                # `frame_placeholder_file_id` — comment + version syncs key
                # on it, and so does the stale-check.
                session.add(
                    FrameLeveranseFolder(
                        notion_page_id=leveranse_page_id,
                        project_page_id=project_page_id,
                        frame_folder_id=discipline_folder_id,
                        frame_placeholder_file_id=placeholder_id,
                        current_name=leveranse_name,
                        current_discipline=discipline_label,
                        frame_url=url,
                    )
                )
            elif row.current_name != leveranse_name:
                # Discipline change is logged but not re-parented — moving the
                # placeholder file across discipline folders requires a V4
                # endpoint we haven't verified safe yet. The placeholder stays
                # under its original discipline and only the filename is
                # updated. Revisit once Frame UI behavior of stale parented
                # files is observed.
                if row.current_discipline != discipline_label:
                    logger.warning(
                        "frame: leveranse %s discipline changed %r→%r — renaming "
                        "in place under old discipline; re-parenting is not "
                        "yet implemented",
                        leveranse_page_id,
                        row.current_discipline,
                        discipline_label,
                    )
                # Under the flattened layout, the placeholder filename IS the
                # visible label in Frame's UI, so a leveranse rename in Notion
                # must rename the file too. The file id is preserved by
                # Frame's PATCH, so the version stack (and cached comment
                # joins) survive the rename.
                renamed = await frame_client.rename_file(
                    row.frame_placeholder_file_id, placeholder_filename
                )
                placeholder_id = row.frame_placeholder_file_id
                url = _file_view_url(renamed, placeholder_id, project_id_for_url)
                row.current_name = leveranse_name
                row.current_discipline = discipline_label
                row.frame_url = url
                await session.merge(row)
                action = "renamed"
            else:
                placeholder_id = row.frame_placeholder_file_id
                url = row.frame_url
                action = "unchanged"

            await session.commit()
    except Exception:
        logger.exception("frame: leveranse sync failed for %s", leveranse_page_id)
        result.action = "failed"
        return result

    try:
        await notion_client.set_task_frame_url(leveranse_page_id, url)
    except Exception:
        logger.exception(
            "frame: failed to patch Frame URL on leveranse page %s",
            leveranse_page_id,
        )

    # Best-effort Oppstart auto-create — independent of the Frame writeback
    # so a Notion outage on the URL patch doesn't skip the Oppgave creation
    # (and vice versa). `_ensure_oppstart_oppgave` is itself best-effort.
    await _ensure_oppstart_oppgave(leveranse_page_id, leveranse_name)

    result.action = action
    # `frame_folder_id` on the result now records the (shared) discipline
    # folder the placeholder lives in; the load-bearing per-leveranse id is
    # the placeholder file.
    result.frame_folder_id = discipline_folder_id
    result.frame_placeholder_file_id = placeholder_id
    result.frame_url = url
    logger.info(
        "frame leveranse sync %s for %r (page %s, discipline %s) → file=%s",
        action,
        leveranse_name,
        leveranse_page_id,
        discipline_label,
        placeholder_id,
    )
    return result
