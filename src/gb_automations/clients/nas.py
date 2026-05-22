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
from datetime import datetime
from pathlib import Path

from gb_automations.config import settings
from gb_automations.utils.labels import project_path_parts

logger = logging.getLogger(__name__)


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


def ensure_project_folders(project_name: str, created_time: str | None) -> Path:
    """Create the project dir and its received subfolder. Idempotent.

    Returns the project dir. Safe to call repeatedly — also heals a folder a
    user deleted by hand.
    """
    target = project_dir(project_name, created_time)
    received = target / settings.nas_received_subfolder
    received.mkdir(parents=True, exist_ok=True)
    logger.info("📁 ensured NAS project folder %s (with %s/)", target, settings.nas_received_subfolder)
    return target


def rename_project_folder(
    old_name: str, new_name: str, created_time: str | None
) -> Path:
    """Rename a project's folder in place when its Notion title changed.

    Falls back to creating the new folder if the old one is missing (self-heal,
    mirroring the Gmail label's 404-heal). Returns the new project dir.
    """
    old_dir = project_dir(old_name, created_time)
    new_dir = project_dir(new_name, created_time)

    if old_dir == new_dir:
        return ensure_project_folders(new_name, created_time)

    if not old_dir.exists():
        logger.warning(
            "↳ NAS rename: old folder %s missing; creating %s fresh", old_dir, new_dir
        )
        return ensure_project_folders(new_name, created_time)

    # If the destination already exists (e.g. a stale folder under the new name),
    # don't clobber it — fold the rename into an ensure so we never destroy data.
    if new_dir.exists():
        logger.warning(
            "↳ NAS rename: target %s already exists; leaving both, ensuring target",
            new_dir,
        )
        return ensure_project_folders(new_name, created_time)

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    os.rename(old_dir, new_dir)
    logger.info("📁 renamed NAS project folder %s → %s", old_dir, new_dir)
    # Ensure the received subfolder exists even if the old folder predated it.
    (new_dir / settings.nas_received_subfolder).mkdir(parents=True, exist_ok=True)
    return new_dir


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
