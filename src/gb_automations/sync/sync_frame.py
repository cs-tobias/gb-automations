"""Project / Leveranse → Frame.io mirror.

Notion is the source of truth; this module reconciles the corresponding
**Frame Project** (one per Notion project, at the workspace level) and the
folder structure inside it:

    <workspace>/<ProjectName> (Frame Project)
                 └── (project's root_folder_id, auto-created by Frame)
                     ├── Interiør/
                     │   ├── <LeveranseA>_<studio>_V00.jpg
                     │   └── <LeveranseB>_<studio>_V00.jpg
                     └── Eksteriør/
                         └── <LeveranseC>_<studio>_V00.jpg

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
and `sync_frame_leveranse` for `frame_leveranse_sync`. Only deliverable rows
(Type is a recognized discipline — Interiør/Eksteriør/Animasjon/Annet — in the
Oppgaver DB) reach the leveranse sync; the webhook gate filters internal tasks
(e.g. Type="Klargjøre modell") out before enqueuing.

Korreksjonsrunde sub-rows are created later by sync_frame_comments (in the
Oppgaver DB, under their deliverable) when Frame comments arrive; the
individual Korreksjon rows land in the Korreksjoner DB.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    DISCIPLINE_KEYS,
    FRAME_DISCIPLINE_FOLDER_NAMES,
    STATUS_KLAR_TIL_OPPSTART,
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

    Shape: `<task>_<studio>_V00.jpg` — e.g. `Vinkel 1_Goldbox.no_V00.jpg`.
    The leveranse name alone (no project prefix) keeps filenames uniquely
    labelled within a shared discipline folder while matching the format
    Goldbox wants to see in Frame (deliverables ship as JPG). The placeholder
    is always V00; the team's first real delivery uploads on top as V01 in
    Frame's version stack.

    The studio slot comes from settings.frame_filename_studio (default
    "Goldbox.no"). Renaming the studio in .env affects only NEW placeholders
    — existing uploads keep their filename (Frame's version stack is keyed
    by file slot, not name). `project_name` is no longer part of the filename
    but is kept in the signature so callers don't need to change.
    """
    studio = settings.frame_filename_studio or "Goldbox.no"
    return f"{task_name}_{studio}_V00.jpg"


def _placeholder_source_url(leveranse_page_id: str) -> str:
    """URL Frame fetches the placeholder bytes from.

    Prefer the per-deliverable dynamic render endpoint
    (`<origin>/assets/placeholder/<page_id>.jpg`), which composes the
    background + description text from the live Notion row. Falls back to the
    static `frame_placeholder_url` when the public origin can't be derived
    (e.g. frame_placeholder_url isn't an absolute URL). Frame fetches this
    asynchronously after create_file_from_url returns, so the URL must stand
    on its own — it carries the page id and the endpoint re-reads Notion.
    """
    base = settings.placeholder_render_base
    if not base:
        return settings.frame_placeholder_url
    return f"{base}/assets/placeholder/{leveranse_page_id}.jpg"

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
    # (and reused its placeholder.jpg if one was present).
    action: str = "skipped"  # created | adopted | renamed | unchanged | skipped | failed
    frame_folder_id: str | None = None
    frame_placeholder_file_id: str | None = None
    frame_url: str | None = None
    note: str | None = None
    # Set True when the engine discovered that the placeholder is already
    # inside a version stack (a real version was uploaded on top of V00)
    # and enqueued a `frame_version_sync` audit to backfill any
    # pre-existing comments. Surfaced in the worker's log line so the
    # operator can see the chain on every Sync click.
    audit_queued: bool = False
    # The version_stack id the audit was enqueued for, if any. Logged
    # alongside `audit_queued`.
    audit_stack_id: str | None = None


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

    Mirror of _find_child_folder_by_name for the placeholder.jpg lookup —
    same list_folder_children call returns folders AND files, we just
    filter the opposite way. We accept any non-folder type because the
    exact V4 file type string isn't pinned anywhere in our wrapper; the
    only sibling of a placeholder.jpg that would slip past this is another
    folder named "placeholder.jpg", which is nonsensical.
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
                    # Adoption means we're now operating inside a project the
                    # client may already have content in. Log what's there so a
                    # first sync is auditable (we never delete/move/overwrite —
                    # only add our discipline folders + V00 placeholders beside
                    # whatever exists).
                    try:
                        preexisting = await frame_client.list_folder_children(
                            root_folder_id
                        )
                        n_folders = sum(
                            1
                            for c in preexisting
                            if c.get("type") in (None, "folder")
                        )
                        logger.info(
                            "frame: adopted existing project %r (id=%s) — "
                            "root holds %d pre-existing entries (%d folders); "
                            "sync only adds alongside, never overwrites",
                            leaf,
                            project_id,
                            len(preexisting),
                            n_folders,
                        )
                    except Exception:
                        logger.exception(
                            "frame: adopted project %s but failed to list "
                            "pre-existing root contents (non-fatal)",
                            project_id,
                        )
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


# How long to wait before checking whether Frame fetched the placeholder
# source URL. Frame's async fetcher typically hits us within 1-2s of
# `create_file_from_url` returning (verified via the `🖼 placeholder
# render` log), so 15s leaves a comfortable margin for slow fetches +
# Frame's internal retries.
_PLACEHOLDER_VERIFY_WAIT_SECONDS = 15.0


# Smallest plausible real placeholder image. The bare black-canvas fallback
# (`render_placeholder(None, None)`) is a few KB even as a JPEG; any text
# render is well above. Any value below this floor is Frame having stored
# an HTTP error body (the bug this guard catches: Frame's fetcher
# occasionally lands an "error code: 1015"-style text payload from the
# Cloudflare tunnel and reports `file_size: 15, media_type: text/plain`
# back to us). 1000 bytes is a comfortable floor that flags those without
# false-positiving on any legitimate render.
_PLACEHOLDER_MIN_BYTES = 1000


def _frame_file_has_media(file_obj: dict) -> tuple[bool, str]:
    """Did Frame's async background fetcher actually ingest our image?

    Returns `(ok, reason)` — `reason` is empty on success, a short
    human-readable cause on failure (used by the WARNING log so operators
    can triage at a glance: did Frame fetch nothing, or fetch garbage?).

    Two checks, both load-bearing — verified live against Frame V4 on
    Goldbox's tenant after a bulk-Sync run where 5/6 placeholders showed
    no UI thumbnail despite Frame returning `status="transcoded"`:

      • `file_size >= _PLACEHOLDER_MIN_BYTES` — guards against the case
        where Frame's fetcher pulled a short HTTP error body (observed:
        `file_size: 15` from a Cloudflare-tunnel hiccup). The previous
        `> 0` check was too lax and let those through.
      • `media_type` starts with `image/` — Frame DERIVES `media_type`
        from the response Content-Type, NOT from the upload filename
        (contrary to an earlier docstring's claim). On a successful
        ingest it's `image/jpeg` (currently) or `image/png` (legacy);
        on the error-body case above it comes back `text/plain`. Same
        broken file flags both signals, but they're independent enough
        that catching either is cheap insurance against a shape drift
        on one side.

    Defensive: if either field is missing entirely (older API surface
    or schema drift), we treat the file as fetched-OK to avoid spuriously
    re-uploading. The whole verify-and-retry path is a workaround for a
    Frame-side race, not a hard correctness gate — a false negative is
    acceptable, a false positive (= no retry when we should retry) is
    what this guard tightens.
    """
    if not isinstance(file_obj, dict):
        return False, "no file payload"

    size = file_obj.get("file_size")
    if size is not None:
        if not isinstance(size, (int, float)) or size < _PLACEHOLDER_MIN_BYTES:
            return False, f"file_size={size}"

    media_type = file_obj.get("media_type")
    if media_type is not None and not str(media_type).startswith("image/"):
        return False, f"media_type={media_type!r}"

    return True, ""


async def _verify_or_reupload_placeholder(
    *,
    placeholder_id: str,
    discipline_folder_id: str,
    filename: str,
    leveranse_page_id: str,
) -> tuple[str, dict]:
    """Wait for Frame to fetch the placeholder source URL, then verify
    the file has media. If Frame dropped the fetch (bulk-concurrency
    race observed under project-level Sync fan-out), delete the empty
    file and re-create it once. Returns the (file_id, file_obj) of the
    final placeholder.

    Single retry: the race is rare enough that one re-upload nearly
    always succeeds. If the second attempt also drops, we leave it as-is
    and log a WARNING — the operator can re-click Sync on the row, or
    spot it in the log and intervene.
    """
    await asyncio.sleep(_PLACEHOLDER_VERIFY_WAIT_SECONDS)
    try:
        file_obj = await frame_client.get_file(placeholder_id)
    except Exception:
        logger.warning(
            "frame: placeholder verify GET failed for %s — assuming OK",
            placeholder_id,
            exc_info=True,
        )
        return placeholder_id, {}

    ok, reason = _frame_file_has_media(file_obj)
    if ok:
        logger.info(
            "frame: placeholder %s for leveranse %s verified — Frame "
            "ingested the source URL ok",
            placeholder_id,
            leveranse_page_id,
        )
        return placeholder_id, file_obj

    logger.warning(
        "frame: placeholder %s for leveranse %s has NO valid media after "
        "%.0fs (%s) — Frame's async fetcher dropped the fetch or stored an "
        "error body. Deleting + re-uploading.",
        placeholder_id,
        leveranse_page_id,
        _PLACEHOLDER_VERIFY_WAIT_SECONDS,
        reason,
    )
    try:
        await frame_client.delete_file(placeholder_id)
    except Exception:
        logger.warning(
            "frame: delete of empty placeholder %s failed — re-upload may "
            "fail with a duplicate-name error",
            placeholder_id,
            exc_info=True,
        )

    new_file = await frame_client.create_file_from_url(
        discipline_folder_id,
        filename,
        _placeholder_source_url(leveranse_page_id),
    )
    new_id = new_file["id"]
    # Second verify — short wait, no retry beyond this. The race is
    # rare; two drops in a row would be exceptional and worth surfacing.
    await asyncio.sleep(_PLACEHOLDER_VERIFY_WAIT_SECONDS)
    try:
        verified = await frame_client.get_file(new_id)
    except Exception:
        return new_id, new_file
    ok2, reason2 = _frame_file_has_media(verified)
    if not ok2:
        logger.warning(
            "frame: placeholder %s for leveranse %s STILL has no valid media "
            "after re-upload (%s) — operator should re-click Sync on this row",
            new_id,
            leveranse_page_id,
            reason2,
        )
    else:
        logger.info(
            "frame: placeholder %s for leveranse %s — re-upload succeeded, "
            "Frame ingested the source URL",
            new_id,
            leveranse_page_id,
        )
    return new_id, verified


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
        logger.info("frame: leveranse %s has no title yet — skipping", leveranse_page_id)
        result.note = "no title yet"
        return result
    if not project_page_id:
        logger.info(
            "frame: leveranse %r has no Prosjekt relation — skipping",
            leveranse_name,
        )
        result.note = "no project relation"
        return result
    if not discipline_label:
        logger.info("frame: leveranse %r has no Type — skipping", leveranse_name)
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
        #   "Vinkel 1_Goldbox.no_V00.jpg"
        # Computed from the leveranse title + the configured studio slot. The
        # "_V00" marks this as the placeholder version; the team's first real
        # delivery uploads on top of it as V01 in Frame's version stack.
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
                        _placeholder_source_url(leveranse_page_id),
                    )
                    placeholder_id = placeholder["id"]
                    # Verify Frame actually fetched the source URL. Under
                    # bulk-Sync fan-out we've observed Frame's async
                    # fetcher dropping the very first leveranse's fetch
                    # (the file is created with the right name + id, but
                    # `🖼 placeholder render` is never logged on our
                    # side → Frame ingested empty bytes → broken preview).
                    # If detected, the helper deletes + re-uploads once;
                    # the (possibly new) id + payload is what we cache.
                    placeholder_id, placeholder = await _verify_or_reupload_placeholder(
                        placeholder_id=placeholder_id,
                        discipline_folder_id=discipline_folder_id,
                        filename=placeholder_filename,
                        leveranse_page_id=leveranse_page_id,
                    )
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
        await notion_client.set_deliverable_frame_url(leveranse_page_id, url)
    except Exception:
        logger.exception(
            "frame: failed to patch Frame URL on deliverable page %s",
            leveranse_page_id,
        )

    # Provisioning a deliverable puts it into the pipeline → set Status to
    # "Klar til oppstart", but ONLY when it has no status yet. A re-click of
    # Sync on a deliverable that's already mid-cycle (Under arbeid / Ferdig /
    # …) must not regress it; and a blank read here means "fresh provision".
    # set_deliverable_status additionally respects MANUAL_DELIVERABLE_STATUSES,
    # so a manual override is never overwritten even if it were somehow blank.
    # Best-effort: a Notion blip here shouldn't fail the Frame sync.
    try:
        current_status = await notion_client.get_deliverable_status(leveranse_page_id)
        if current_status is None:
            await notion_client.set_deliverable_status(
                leveranse_page_id, STATUS_KLAR_TIL_OPPSTART
            )
    except Exception:
        logger.exception(
            "frame: failed to set initial Status on deliverable page %s "
            "(non-fatal)",
            leveranse_page_id,
        )

    result.action = action
    # `frame_folder_id` on the result now records the (shared) discipline
    # folder the placeholder lives in; the load-bearing per-leveranse id is
    # the placeholder file.
    result.frame_folder_id = discipline_folder_id
    result.frame_placeholder_file_id = placeholder_id
    result.frame_url = url

    # Self-heal hook: if a real version has already been uploaded on top
    # of this V00 placeholder (i.e. the placeholder's parent is now a
    # version stack instead of the bare discipline folder), enqueue a
    # `frame_version_sync` so the stack audit picks up any pre-existing
    # comments. This is what makes the per-project / per-deliverable
    # "Sync" button reconcile Frame state that was created BEFORE the
    # integration was hooked up — without it, the audit only runs on
    # live `file.versioned` webhooks, leaving prior content invisible.
    #
    # Always-enqueue (per the user's intent for the manual Sync button:
    # "self-heal whenever you press Sync"). The audit is idempotent via
    # FrameComment ON CONFLICT DO NOTHING + the FrameKorreksjonsrunde
    # cache + set_deliverable_status read-first; the active-dedup index
    # on sync_tasks collapses simultaneous re-clicks to one task.
    #
    # Best-effort: a Frame blip here must NOT fail the leveranse sync
    # (the placeholder write already succeeded). Lazy import the queue
    # helper + worker wake to avoid a sync_frame ↔ queue import cycle.
    try:
        file_obj = await frame_client.get_file(placeholder_id)
        parent_id = file_obj.get("parent_id") if isinstance(file_obj, dict) else None
        if parent_id and parent_id != discipline_folder_id:
            # The placeholder's parent is something other than the bare
            # discipline folder → probe whether it's a version stack.
            # `list_version_stack_children` 422s when called on a folder,
            # so a successful response confirms a stack.
            try:
                stack_children = await frame_client.list_version_stack_children(parent_id)
            except frame_client.FrameAPIError as err:
                if getattr(err, "status_code", None) in (404, 422):
                    stack_children = None  # parent is a folder, no stack
                else:
                    raise
            if stack_children:
                # A stack exists. Enqueue the audit — even when this is a
                # no-op re-sync, idempotency makes that cheap.
                from gb_automations.jobs import queue_worker
                from gb_automations.sync.queue import enqueue_frame_version_sync

                await enqueue_frame_version_sync(parent_id)
                queue_worker.wake()
                result.audit_queued = True
                result.audit_stack_id = parent_id
    except Exception:
        logger.warning(
            "frame: stack-audit probe failed for placeholder %s (leveranse %s) "
            "— leveranse sync still succeeded, audit was NOT enqueued",
            placeholder_id,
            leveranse_page_id,
            exc_info=True,
        )

    logger.info(
        "frame leveranse sync %s for %r (page %s, discipline %s) → file=%s%s",
        action,
        leveranse_name,
        leveranse_page_id,
        discipline_label,
        placeholder_id,
        f" + audit queued for stack {result.audit_stack_id}" if result.audit_queued else "",
    )
    return result
