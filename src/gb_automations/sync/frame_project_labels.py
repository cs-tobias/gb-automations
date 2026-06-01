"""Project-name resolution for Frame.io log lines.

Every Frame engine log line that names a stack/file/leveranse should also
name the Frame project so an operator scanning the noisy comment-firehose
logs can tell, at a glance, which client project is involved.

Two paths:

  - `frame_project_label_for_page(notion_project_page_id)` — the cheap
    Postgres lookup against `FrameProjectFolder.current_name`. Used by
    engines that already have the Notion project page id resolved (the
    tracked-leveranse path: stack audit, version sync, the live
    comment path on a tracked file).

  - `frame_project_label_for_file(frame_file_id)` — the fallback for
    UNTRACKED files (a client commenting on a file we don't manage).
    Walks `get_file` → reads `project_id` from the payload if present,
    falls back to walking up the folder ancestry, and resolves through
    `FrameProjectFolder` by `frame_project_id` for free, or calls
    Frame's `get_project` as a last resort. Results are memoized in a
    process-local LRU so a comment-firehose burst on the same file
    costs one round-trip total — not durable across restarts, doesn't
    need to be.

Both helpers return a short string label suitable for `project=%s` in a
log message. They never raise — a resolution failure becomes the literal
`'(unknown)'` so it doesn't shadow the actual error the caller is logging
about.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.db import SessionLocal
from gb_automations.models import FrameProjectFolder

logger = logging.getLogger(__name__)

# Bounded LRU for file_id → project label. Process-local; restarts re-warm.
# A typical noisy untracked file (the f8b82347 / d914bd01 case from the live
# logs) fires dozens of webhook events per session — one round-trip total
# per file is the goal, not one per comment.
_FILE_LABEL_CACHE_SIZE = 512
_file_label_cache: OrderedDict[str, str] = OrderedDict()
_file_label_lock = asyncio.Lock()


def _cache_put(key: str, value: str) -> None:
    if key in _file_label_cache:
        _file_label_cache.move_to_end(key)
    else:
        _file_label_cache[key] = value
        if len(_file_label_cache) > _FILE_LABEL_CACHE_SIZE:
            _file_label_cache.popitem(last=False)


def _cache_get(key: str) -> str | None:
    if key not in _file_label_cache:
        return None
    _file_label_cache.move_to_end(key)
    return _file_label_cache[key]


async def frame_project_label_for_page(
    notion_project_page_id: str | None,
) -> str:
    """Return the Frame project's `current_name` (cached) for a Notion
    project page id, or `'(unknown)'` if there's no row.

    Hot path — every audit/version log line will call this. Postgres
    read by primary key.
    """
    if not notion_project_page_id:
        return "(unknown)"
    try:
        async with SessionLocal() as session:
            row = await session.get(FrameProjectFolder, notion_project_page_id)
    except Exception:  # noqa: BLE001
        # Logging should never crash the caller. Swallow.
        return "(unknown)"
    if row is None:
        return "(unknown)"
    return row.current_name or "(unknown)"


async def frame_project_label_for_file(frame_file_id: str | None) -> str:
    """Resolve a Frame file's project name without a Notion-side anchor.

    Used for the noisy untracked-file log lines: a comment fired on a
    file the client uploaded organically (outside the integration's
    managed folder structure), so there's no FrameLeveranseFolder row
    to look up. We still want the operator to see WHICH project the
    noise belongs to.

    Resolution chain (each step is best-effort, returns `'(unknown)'`
    on any error):

      1. In-memory cache by file id.
      2. `get_file(file_id)` → check `project_id` on the payload.
         When present, try `FrameProjectFolder` by `frame_project_id`
         (the cache covers projects we manage — free). Otherwise
         `get_project(project_id)` for the name.
      3. If `project_id` isn't on the file payload (older API or
         non-tracked files), give up and return `'(unknown)'` rather
         than walking parents — the cost-benefit doesn't justify a
         folder-traversal loop on a log helper.
    """
    if not frame_file_id:
        return "(unknown)"

    cached = _cache_get(frame_file_id)
    if cached is not None:
        return cached

    async with _file_label_lock:
        # Re-check under the lock — a concurrent task may have populated
        # the cache while we were waiting.
        cached = _cache_get(frame_file_id)
        if cached is not None:
            return cached

        label = await _resolve_label_for_file(frame_file_id)
        _cache_put(frame_file_id, label)
        return label


async def _resolve_label_for_file(frame_file_id: str) -> str:
    try:
        file_obj = await frame_client.get_file(frame_file_id)
    except Exception:  # noqa: BLE001
        return "(unknown)"

    project_id = file_obj.get("project_id") if isinstance(file_obj, dict) else None
    if not project_id:
        return "(unknown)"

    # Tracked project fast-path: scan FrameProjectFolder by the Frame
    # project id (the column is not indexed; this is a tiny table).
    try:
        async with SessionLocal() as session:
            stmt = select(FrameProjectFolder).where(
                FrameProjectFolder.frame_project_id == project_id
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
    except Exception:  # noqa: BLE001
        row = None
    if row is not None and row.current_name:
        return row.current_name

    # Untracked project — ask Frame directly. One round-trip per unique
    # untracked file (memoized above).
    try:
        proj = await frame_client.get_project(project_id)
    except Exception:  # noqa: BLE001
        return "(unknown)"
    name = proj.get("name") if isinstance(proj, dict) else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return "(unknown)"


__all__ = [
    "frame_project_label_for_file",
    "frame_project_label_for_page",
]
