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
