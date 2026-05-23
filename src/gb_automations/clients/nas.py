"""Office NAS client — project folders on the shared `W:` drive.

The Docker host is an office workstation on the same LAN as the NAS, with the
share mounted into the container (see docs/nas-setup.md). So everything here is
plain filesystem I/O against `settings.nas_projects_root` — no SMB library, VPN,
or remote protocol involved.

Folder scheme mirrors Goldbox's existing layout:
    <nas_projects_root>/<year>/<project-name>/<received-subfolder>/
e.g. W:\\Prosjekt\\2026\\1187_Heimdal_Solsletta bygg D\\Mottatt

The `<year>/<project-name>` parts come from utils.labels.project_path_parts, the
same source the Gmail label uses, so the folder leaf and the label leaf are
byte-identical. The received subfolder ("Mottatt" — where incoming client/email
files go) is created up front so Milestone 2 (attachments → NAS) has a target.

These functions are synchronous; callers wrap them in asyncio.to_thread, like
the Gmail and Drive clients.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from gb_automations.config import (
    DISCIPLINE_FOLDER_MEDIA,
    DISCIPLINE_FOLDER_SCENES,
    DISCIPLINE_KEYS,
    settings,
)
from gb_automations.utils.labels import _sanitize_leaf, project_path_parts

logger = logging.getLogger(__name__)


# Goldbox's project folder template ("Mappestruktur"). Encoded as data, not read
# from the live template folder on the NAS, so we never replicate the OS noise
# (.DS_Store/Thumbs.db) that lives there and so discipline filtering is explicit.
# Paths are "/"-relative to the project dir; created with mkdir(parents=True).
#
# ALWAYS-CREATED — every project gets these regardless of discipline. ("Mottatt"
# is created separately, the same as before this template existed.) The whole
# Master subtree is here, NOT under the conditional set: its "Interioer" is a
# fixed folder, unrelated to the Interiør discipline.
_ALWAYS_FOLDERS: tuple[str, ...] = (
    "Arbeidsfiler/3ds max/autoback",
    "Arbeidsfiler/3ds max/export",
    "Arbeidsfiler/3ds max/sceneassets/images",
    "Arbeidsfiler/3ds max/sceneassets/material libraries",
    "Arbeidsfiler/3ds max/sceneassets/objects",
    "Arbeidsfiler/3ds max/sceneassets/render presets",
    "Arbeidsfiler/3ds max/scenes/Trinn 01/Master/Bygg/Versjoner",
    "Arbeidsfiler/3ds max/scenes/Trinn 01/Master/Interioer/Versjoner",
    "Arbeidsfiler/3ds max/scenes/Trinn 01/Master/Utomhus/Versjoner",
    "Arbeidsfiler/After effects/arbeidsfiler",
    "Arbeidsfiler/After effects/assets",
    "Arbeidsfiler/Blender/Blender filer",
    "Arbeidsfiler/Blender/Export",
    "Arbeidsfiler/Blender/Maps filer",
    "Arbeidsfiler/Marvelous/arbeidsfiler",
    "Arbeidsfiler/Marvelous/export",
    "Arbeidsfiler/Metashape/arbeidsfiler",
    "Arbeidsfiler/Metashape/export",
    "Arbeidsfiler/PureRef",
    "Arbeidsfiler/Studio Scene",
    "Arbeidsfiler/Syntheyes/arbeidsfiler",
    "Arbeidsfiler/Syntheyes/export",
    "Arbeidsfiler/Unreal",
    "Arbeidsfiler/Zbrush/arbeidsfiler",
    "Arbeidsfiler/Zbrush/export",
    "Media/Finals",
    "Media/Footage/Trinn 01",
    "Media/Photoshop",
    "Media/Preview",
    "Media/Render",
)

# DISCIPLINE-CONDITIONAL parents: (parent path, which folder-name table to use).
# For each project discipline, a child folder is created under each parent using
# the table — "scenes" → DISCIPLINE_FOLDER_SCENES (Animation), "media" →
# DISCIPLINE_FOLDER_MEDIA (Animasjon). Disciplines the project lacks are skipped.
_DISCIPLINE_PARENTS: tuple[tuple[str, str], ...] = (
    ("Arbeidsfiler/3ds max/scenes/Trinn 01", "scenes"),
    ("Media/Finals/Trinn 01", "media"),
    ("Media/Photoshop/Trinn 01", "media"),
    ("Media/Preview/Trinn 01", "media"),
    ("Media/Render/Trinn 01", "media"),
)


def normalize_disciplines(labels: Iterable[str]) -> set[str]:
    """Map Notion `Type` labels to canonical keys, dropping unknowns.

    >>> sorted(normalize_disciplines(["Interiør", " Animasjon "]))
    ['animation', 'interior']
    """
    keys: set[str] = set()
    for label in labels:
        key = DISCIPLINE_KEYS.get((label or "").strip().lower())
        if key:
            keys.add(key)
        elif label and label.strip():
            logger.warning("unknown discipline label %r — ignoring", label)
    return keys


def _planned_folders(disciplines: set[str]) -> list[str]:
    """All folders to create for a project with these (canonical) disciplines."""
    folders = list(_ALWAYS_FOLDERS)
    for parent, kind in _DISCIPLINE_PARENTS:
        table = DISCIPLINE_FOLDER_SCENES if kind == "scenes" else DISCIPLINE_FOLDER_MEDIA
        for key in disciplines:
            folders.append(f"{parent}/{table[key]}")
    return folders


def _root() -> Path:
    if not settings.nas_projects_root:
        raise RuntimeError("NAS_PROJECTS_ROOT is not configured")
    return Path(settings.nas_projects_root)


def nas_available() -> bool:
    """True if the NAS root is configured and is a writable directory.

    Lets the webhook short-circuit and report cleanly when the share is
    unmounted or down, rather than throwing mid-create. A mounted-but-readonly
    share (wrong uid on a CIFS mount — the classic footgun, see gotchas.md)
    also fails here.
    """
    if not settings.nas_projects_root:
        return False
    root = _root()
    return root.is_dir() and os.access(root, os.W_OK)


def project_dir(project_name: str, created_time: str | None) -> Path:
    """Absolute path to a project's folder on the NAS (does not create it)."""
    year, leaf = project_path_parts(project_name, created_time)
    return _root() / year / leaf


def ensure_project_folders(
    project_name: str,
    created_time: str | None,
    disciplines: Iterable[str] | None = None,
) -> Path:
    """Create the project's full folder structure on the NAS. Idempotent.

    Builds Goldbox's template tree (`_ALWAYS_FOLDERS` + the received "Mottatt"
    folder) plus the discipline-conditional Trinn 01 subfolders for whichever
    of `disciplines` the project has. `disciplines` are the raw Notion labels
    (Interiør/Eksteriør/Animasjon); unknown/empty values are ignored, so a
    project with no disciplines just gets the always-folders.

    Create-only and safe to call repeatedly: every folder is mkdir(exist_ok),
    nothing is enumerated or removed. Re-running after a discipline was ADDED in
    Notion creates the new branches; a discipline REMOVED in Notion leaves its
    existing folders untouched (we never delete — they may hold work). Also
    heals a folder a user deleted by hand. Returns the project dir.
    """
    target = project_dir(project_name, created_time)
    keys = normalize_disciplines(disciplines or [])

    target.mkdir(parents=True, exist_ok=True)
    (target / settings.nas_received_subfolder).mkdir(parents=True, exist_ok=True)
    for rel in _planned_folders(keys):
        (target / Path(rel)).mkdir(parents=True, exist_ok=True)

    logger.info(
        "📁 ensured NAS project structure %s (disciplines=%s)",
        target,
        ", ".join(sorted(keys)) or "none",
    )
    return target


def rename_project_folder(
    old_name: str,
    new_name: str,
    created_time: str | None,
    disciplines: Iterable[str] | None = None,
) -> Path:
    """Rename a project's folder in place when its Notion title changed.

    Falls back to creating the new folder if the old one is missing (self-heal,
    mirroring the Gmail label's 404-heal). After a successful in-place rename we
    also re-ensure the structure, so disciplines added in Notion since the last
    sync get their folders even on a rename. Returns the new project dir.
    """
    old_dir = project_dir(old_name, created_time)
    new_dir = project_dir(new_name, created_time)

    if old_dir == new_dir:
        return ensure_project_folders(new_name, created_time, disciplines)

    if not old_dir.exists():
        logger.warning(
            "↳ NAS rename: old folder %s missing; creating %s fresh", old_dir, new_dir
        )
        return ensure_project_folders(new_name, created_time, disciplines)

    # If the destination already exists (e.g. a stale folder under the new name),
    # don't clobber it — fold the rename into an ensure so we never destroy data.
    if new_dir.exists():
        logger.warning(
            "↳ NAS rename: target %s already exists; leaving both, ensuring target",
            new_dir,
        )
        return ensure_project_folders(new_name, created_time, disciplines)

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old_dir, new_dir)
    logger.info("📁 renamed NAS project folder %s → %s", old_dir, new_dir)
    # Re-ensure the full structure under the new name: keeps the received folder
    # present even if the old folder predated it, and fills in any discipline
    # branches added since the folder was first created.
    return ensure_project_folders(new_name, created_time, disciplines)


# ─── Task folders ───────────────────────────────────────────────────────────
#
# A Notion task ("Oppgaver" row) gets one folder per project-discipline parent
# whose discipline matches the task's. The set of parents IS the project's
# `_DISCIPLINE_PARENTS` — using them keeps task folders aligned with the project
# structure for free (and lets Frame.io reuse the same spec later).


def _task_parent_dirs(project_dir_path: Path, discipline_key: str) -> list[Path]:
    """Return the 5 absolute parents a task folder lives under for a discipline.

    `discipline_key` is the canonical key ('interior'/'exterior'/'animation').
    Each parent dir = <project>/<discipline-parent>/<discipline-folder-name>,
    where the per-kind name table picks 'Animation' under scenes vs 'Animasjon'
    under Media (the same location quirk the project layer encodes).
    """
    parents: list[Path] = []
    for parent_rel, kind in _DISCIPLINE_PARENTS:
        table = DISCIPLINE_FOLDER_SCENES if kind == "scenes" else DISCIPLINE_FOLDER_MEDIA
        folder = table.get(discipline_key)
        if folder is None:
            # discipline_key not in the table is a programming error (caller
            # should have normalized first). Skip rather than crash.
            continue
        parents.append(project_dir_path / Path(parent_rel) / folder)
    return parents


def ensure_task_folders(
    project_name: str,
    created_time: str | None,
    task_name: str,
    discipline_label: str,
) -> list[Path]:
    """Create the task's folders under the project's discipline branches.

    `discipline_label` is the raw Notion `Type` option (e.g. "Interiør");
    we normalize via DISCIPLINE_KEYS. Unknown/empty disciplines yield an empty
    list (logged), so the caller can decide whether that's a soft skip. The
    task name is `_sanitize_leaf`'d to keep SMB-illegal chars off the share —
    the same sanitizer the project leaf uses, so a task and its folder name
    stay byte-identical.

    Idempotent: every dir is mkdir(exist_ok). Returns the list of task dirs
    (created or pre-existing).
    """
    keys = normalize_disciplines([discipline_label])
    if not keys:
        return []
    key = next(iter(keys))  # tasks have exactly one discipline
    leaf = _sanitize_leaf(task_name)
    if not leaf:
        logger.warning("task name %r sanitizes to empty; nothing to create", task_name)
        return []
    proj_dir = project_dir(project_name, created_time)
    task_dirs: list[Path] = []
    for parent in _task_parent_dirs(proj_dir, key):
        task_dir = parent / leaf
        task_dir.mkdir(parents=True, exist_ok=True)
        task_dirs.append(task_dir)
    logger.info(
        "📁 ensured %d NAS task folder(s) for %r (discipline=%s) under %s",
        len(task_dirs),
        leaf,
        key,
        proj_dir.name,
    )
    return task_dirs


def rename_task_folders(
    project_name: str,
    created_time: str | None,
    old_task_name: str,
    new_task_name: str,
    old_discipline_label: str,
    new_discipline_label: str,
) -> list[Path]:
    """Move a task's folders in place when its name and/or discipline changed.

    For each of the 5 NEW parents (driven by `new_discipline_label`):
      - if the matching OLD folder exists AND the NEW path doesn't → os.rename;
      - otherwise → mkdir(exist_ok) on the new path.
    Files in an old folder we couldn't move (e.g. NEW target already exists)
    are LEFT IN PLACE — we never delete on the live NAS. The old folder is
    visibly orphaned for the user to deal with; nothing is silently lost.

    Returns the list of new task dirs (the canonical target of each parent).
    """
    new_keys = normalize_disciplines([new_discipline_label])
    if not new_keys:
        return []
    new_key = next(iter(new_keys))
    new_leaf = _sanitize_leaf(new_task_name)
    if not new_leaf:
        logger.warning(
            "new task name %r sanitizes to empty; nothing to do", new_task_name
        )
        return []
    proj_dir = project_dir(project_name, created_time)

    # Compute the matching old-parent for each new-parent so the move is
    # parent-pair aware. We rely on _DISCIPLINE_PARENTS being a fixed-order
    # tuple — _task_parent_dirs walks it in the same order both times.
    old_parents: list[Path] = []
    old_keys = normalize_disciplines([old_discipline_label])
    if old_keys:
        old_key = next(iter(old_keys))
        old_parents = _task_parent_dirs(proj_dir, old_key)
    new_parents = _task_parent_dirs(proj_dir, new_key)
    old_leaf = _sanitize_leaf(old_task_name)

    task_dirs: list[Path] = []
    for idx, new_parent in enumerate(new_parents):
        new_task_dir = new_parent / new_leaf
        old_task_dir = (
            old_parents[idx] / old_leaf if idx < len(old_parents) and old_leaf else None
        )

        if (
            old_task_dir is not None
            and old_task_dir != new_task_dir
            and old_task_dir.exists()
            and not new_task_dir.exists()
        ):
            new_task_dir.parent.mkdir(parents=True, exist_ok=True)
            os.rename(old_task_dir, new_task_dir)
            logger.info("📁 renamed NAS task folder %s → %s", old_task_dir, new_task_dir)
        else:
            new_task_dir.mkdir(parents=True, exist_ok=True)
            if (
                old_task_dir is not None
                and old_task_dir != new_task_dir
                and old_task_dir.exists()
            ):
                logger.warning(
                    "↳ task rename: target %s already exists; leaving old %s untouched",
                    new_task_dir,
                    old_task_dir,
                )
        task_dirs.append(new_task_dir)
    return task_dirs


# Windows/SMB-illegal characters in a filename. Email attachment names come from
# arbitrary senders, so a name like `Q3: report?.pdf` would fail os I/O on the
# (Windows-hosted, CIFS-mounted) NAS — sanitize before writing.
_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitize_filename(name: str) -> str:
    cleaned = _ILLEGAL_FILENAME_CHARS.sub("_", name).strip().rstrip(".")
    return cleaned or "attachment"


def received_subfolder_name(date: datetime, tags: list[str]) -> str:
    """Build the dated/tagged subfolder name for a Mottatt copy.

    Format: ``<YYYY-MM-DD> - <Tag1> <Tag2> …`` — each tag capitalized (first
    char upper, rest unchanged), single-space joined, in the order the LLM
    returned them. Goldbox groups incoming files by "date - topic" this way.
    An untagged email defaults to "Underlag" (briefing material) so its files
    still land in a dated folder rather than loose in Mottatt.

    Uses `t[:1].upper() + t[1:]`, not `str.capitalize()`, because capitalize()
    lower-cases the rest — same result for today's all-lowercase taxonomy but
    safe if a tag like "FDV" is ever added. Tag values come from EMAIL_TAGS
    (controlled; the æ/ø/å in them are valid on the CIFS/utf8 mount); the
    result still runs through the illegal-char sanitizer as defense in depth.
    """
    cleaned = [t.strip() for t in tags if t and t.strip()]
    if not cleaned:
        cleaned = ["Underlag"]
    capped = [t[:1].upper() + t[1:] for t in cleaned]
    name = f"{date.strftime('%Y-%m-%d')} - {' '.join(capped)}"
    return _ILLEGAL_FILENAME_CHARS.sub("_", name).strip().rstrip(".")


def write_to_received(
    received_dir: Path,
    filename: str,
    content: bytes,
    *,
    subfolder: str | None = None,
) -> Path | None:
    """Write one attachment into a project's received ("Mottatt") folder.

    `subfolder`, when given, nests the file one level deeper (Goldbox's
    "<date> - <tags>" grouping). Creating it on demand with exist_ok gives
    same-date+same-tagset reuse and new-date/new-tagset isolation for free.

    Content-idempotent and non-clobbering, mirroring the Drive upload's sha1
    dedup at the filesystem level:
      - if a same-named file with the SAME bytes already exists → skip (a re-sync
        or a re-carried quote shouldn't pile up duplicates), return None.
      - if a same-named file with DIFFERENT bytes exists → write `name (2).ext`,
        `name (3).ext`, … so a genuinely different file is never lost or clobbered
        (we never overwrite a file a user may have placed there by hand).

    Returns the path written, or None if the identical file was already present.
    Synchronous filesystem I/O — callers wrap it in asyncio.to_thread.
    """
    target_dir = received_dir / subfolder if subfolder else received_dir
    target_dir.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_filename(filename)
    stem, dot, ext = safe.partition(".")
    suffix = f"{dot}{ext}" if dot else ""

    candidate = target_dir / safe
    n = 1
    while candidate.exists():
        if candidate.stat().st_size == len(content) and candidate.read_bytes() == content:
            return None  # identical file already there — idempotent no-op
        n += 1
        candidate = target_dir / f"{stem} ({n}){suffix}"

    candidate.write_bytes(content)
    return candidate
