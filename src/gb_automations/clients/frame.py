"""Async Frame.io V4 REST client.

Mirrors the shape of `clients/notion.py` — same httpx + retry pattern, same
event-hook logging, same `_raise_for_status` ergonomics. Auth is layered on
via `frame_auth.get_access_token()` so each request carries a freshly-
checked bearer token.

V4 resource hierarchy (per Adobe's docs):
    Account → Workspace → Project → Folder → (Folder | Version Stack | File)

Every asset (image, video, PDF, …) is a `File`. Folders and Version Stacks
are just containers; they have IDs and can be navigated like any other
resource. Comments attach to Files (or to specific timecode/region within
a File).

This module starts with only what we need to prove the connection works:
    - whoami()          — GET /me  (smoke test: refresh token still valid, scopes correct)
    - list_accounts()   — accounts the authenticated user can see
    - list_workspaces() — workspaces within an account

Project/folder/file/comment CRUD lands as we wire up real flows. Don't
speculatively add methods — we'll learn the right shape by reading actual
V4 responses, not by guessing from docs.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from gb_automations.clients import frame_auth

logger = logging.getLogger(__name__)


FRAME_API_BASE = "https://api.frame.io/v4"
# Frame.io's API responds quickly for reads (~200-500ms) but uploads + bulk
# operations can be slower. 30s mirrors the Notion budget and covers worst
# cases without masking real outages.
_HTTP_TIMEOUT = 30.0


class FrameAPIError(RuntimeError):
    """Frame.io HTTP error with body attached for diagnosis."""

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        message = (
            f"Frame.io {response.status_code} "
            f"{response.request.method} {response.url}: {body}"
        )
        super().__init__(message)
        self.status_code = response.status_code
        self.body = body


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise FrameAPIError(response)


async def _with_retries(
    operation: Callable[[], Awaitable[httpx.Response]],
    *,
    op_name: str,
    max_attempts: int = 3,
) -> httpx.Response:
    """Exponential backoff on transient httpx errors + 5xx + 429.

    429 deserves special handling — Frame.io returns `x-ratelimit-*` headers
    we should honor. For now we use the same simple backoff as elsewhere and
    add a Retry-After-aware path only if we see real 429s in production.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await operation()
        except (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
        ) as err:
            last_err = err
            if attempt + 1 >= max_attempts:
                break
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "frame %s attempt %d failed (%s); retrying in %.1fs",
                op_name,
                attempt + 1,
                type(err).__name__,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        if (
            response.status_code == 429 or 500 <= response.status_code < 600
        ) and attempt + 1 < max_attempts:
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "frame %s attempt %d got %d; retrying in %.1fs",
                op_name,
                attempt + 1,
                response.status_code,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        return response
    assert last_err is not None
    raise last_err


async def _log_request(request: httpx.Request) -> None:
    logger.debug("frame → %s %s", request.method, request.url.path)


async def _log_response(response: httpx.Response) -> None:
    logger.debug(
        "frame ← %d %s %s",
        response.status_code,
        response.request.method,
        response.request.url.path,
    )


async def _client(*, access_token: str) -> httpx.AsyncClient:
    """Build an httpx client pre-loaded with auth + logging hooks.

    Caller obtains the access token via `frame_auth.get_access_token()` —
    keeping that out of this constructor means tests can pass a stub token
    without hitting Adobe IMS.
    """
    return httpx.AsyncClient(
        base_url=FRAME_API_BASE,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            # Adobe API gateway uses x-api-key alongside the bearer token to
            # identify the calling integration (separate from auth). Frame.io
            # requires it on every V4 request — without it, valid bearers get
            # 401 from the gateway before reaching Frame.io.
            "x-api-key": _client_id(),
        },
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )


def _client_id() -> str:
    from gb_automations.config import settings

    if not settings.frame_client_id:
        raise RuntimeError(
            "FRAME_CLIENT_ID is not configured "
            "(Adobe Developer Console → your project → Credentials)"
        )
    return settings.frame_client_id


# ============================================================
# Reads — used by bootstrap + /debug/frame
# ============================================================


# A defensive cap so a malformed cursor (one that never advances or loops back)
# can't spin forever. 50 pages * the V4 default page size is far more than any
# real Goldbox workspace/folder will ever hold.
_MAX_PAGES = 50


async def _get_all_pages(
    client: httpx.AsyncClient, path: str, *, op_name: str
) -> list[dict[str, Any]]:
    """GET every page of a V4 list endpoint and return the concatenated `data`.

    The adopt-by-name logic MUST see every existing project/folder — if a
    match falls on page 2 and we only read page 1, we'd create a duplicate
    instead of adopting. So we follow the cursor to exhaustion.

    V4 paginates via a `links.next` (a full or relative URL to the next page)
    and/or a `cursor`/`next_cursor` token echoed under `links`/`pagination`.
    We handle both shapes and a `?page=N` fallback, stopping when no further
    cursor is offered. The page cap (`_MAX_PAGES`) guards against a cursor that
    never terminates.
    """
    items: list[dict[str, Any]] = []
    next_path: str | None = path
    seen_paths: set[str] = set()
    for _ in range(_MAX_PAGES):
        if not next_path or next_path in seen_paths:
            break
        seen_paths.add(next_path)
        response = await _with_retries(
            lambda p=next_path: client.get(p), op_name=op_name
        )
        _raise_for_status(response)
        payload = response.json()
        items.extend(payload.get("data", []))

        links = payload.get("links") or {}
        pagination = payload.get("pagination") or {}
        # `links.next` may be an absolute URL — strip the base so it stays a
        # path the client (with its base_url) can re-issue.
        nxt = links.get("next")
        if isinstance(nxt, dict):
            nxt = nxt.get("href") or nxt.get("url")
        if isinstance(nxt, str) and nxt:
            # Strip the base URL if Frame returns an absolute next link.
            # Frame also returns relative paths like /v4/accounts/... —
            # strip the leading /v4 so httpx doesn't double it with the
            # /v4 already in FRAME_API_BASE (the client's base_url).
            stripped = nxt.replace(FRAME_API_BASE, "")
            if stripped.startswith("/v4/"):
                stripped = stripped[3:]  # /v4/accounts/... → /accounts/...
            next_path = stripped or None
            continue
        cursor = (
            pagination.get("next_cursor")
            or pagination.get("cursor")
            or links.get("next_cursor")
        )
        if cursor:
            sep = "&" if "?" in path else "?"
            next_path = f"{path}{sep}cursor={cursor}"
            continue
        next_path = None
    else:
        logger.warning(
            "frame %s hit the %d-page cap — adoption may have missed entries",
            op_name,
            _MAX_PAGES,
        )
    return items


async def whoami() -> dict[str, Any]:
    """`GET /v4/me` — returns the authenticated user's profile.

    Smoke test for the full auth chain: refresh token valid, IMS exchange
    works, x-api-key recognized, V4 gateway responds. Used by /debug/frame
    and by the bootstrap script after the OAuth dance to confirm everything
    lined up.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get("/me"), op_name="whoami"
        )
        _raise_for_status(response)
        return response.json()


async def list_accounts() -> list[dict[str, Any]]:
    """Accounts the authenticated user can act on.

    For Goldbox there's typically one (`Goldbox`) but the bootstrap script
    surfaces the full list so the right one is picked deliberately rather
    than assumed.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get("/accounts"), op_name="list_accounts"
        )
        _raise_for_status(response)
        return response.json().get("data", [])


async def list_workspaces(account_id: str) -> list[dict[str, Any]]:
    """Workspaces within an account.

    Workspaces are Frame.io's top-level project containers — Goldbox likely
    has one (`Goldbox`) but creative agencies often partition by client.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(f"/accounts/{account_id}/workspaces"),
            op_name="list_workspaces",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


async def list_projects(account_id: str, workspace_id: str) -> list[dict[str, Any]]:
    """Frame.io Projects inside a workspace.

    sync_frame uses this to find existing same-name projects before creating
    a duplicate (adopt-by-name pattern). Each Notion project corresponds to
    one Frame Project, listed here in the workspace's Active Projects view.
    Returns the concatenated `data` across all pages — each item has at least
    `id` and `name`, plus typically `root_folder_id` and `view_url`.

    Paginated to exhaustion: adoption must see EVERY project, or a match on a
    later page is missed and we'd create a duplicate project alongside the
    client's existing one.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        return await _get_all_pages(
            client,
            f"/accounts/{account_id}/workspaces/{workspace_id}/projects",
            op_name="list_projects",
        )


async def create_project(workspace_id: str, name: str) -> dict[str, Any]:
    """Create a top-level Frame Project under a workspace. Returns the new
    project object (including id, name, root_folder_id, view_url).

    V4 endpoint: POST /v4/accounts/{aid}/workspaces/{wid}/projects.

    The response carries a `root_folder_id` (the auto-created root folder
    that becomes the parent of every folder we provision inside this
    project). It's immutable for the lifetime of the Project and is what
    sync_frame_leveranse uses as the parent when creating discipline subfolders.
    """
    token = await frame_auth.get_access_token()
    body = {"data": {"name": name}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.post(
                f"/accounts/{_account_id()}/workspaces/{workspace_id}/projects",
                json=body,
            ),
            op_name="create_project",
        )
        _raise_for_status(response)
        project = _unwrap(response.json())
        logger.info(
            "frame project created %r in workspace %s (id=%s)",
            name,
            workspace_id,
            project.get("id"),
        )
        return project


async def rename_project(project_id: str, new_name: str) -> dict[str, Any]:
    """Rename a Frame Project in place. The project id (and its root_folder_id)
    are preserved, so cached child-folder ids underneath stay valid.

    V4 endpoint: PATCH /v4/accounts/{aid}/projects/{pid}. Same body shape as
    rename_folder."""
    token = await frame_auth.get_access_token()
    body = {"data": {"name": new_name}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/accounts/{_account_id()}/projects/{project_id}",
                json=body,
            ),
            op_name="rename_project",
        )
        _raise_for_status(response)
        project = _unwrap(response.json())
        logger.info("frame project renamed %s → %s", project_id, new_name)
        return project


async def set_project_status(
    project_id: str, status: str
) -> dict[str, Any]:
    """Set the Frame.io project's lifecycle status to "active" or "inactive".

    V4 endpoint: PATCH /v4/accounts/{aid}/projects/{pid} with body
    `{"data": {"status": "active"|"inactive"}}`. The Update Project endpoint
    accepts `name`, `restricted`, and `status` — same path as rename_project,
    just a different body field. Reversible: a previously-inactivated project
    flips back with `status="active"` and stays fully editable while
    inactive (Frame's "Managing Active and Inactive Projects" replaces the
    legacy "archive" feature in V4). Returns the updated project object so
    the caller can log the resulting state.

    Loop-prevention: the caller (`sync_frame_project_status`) reads Frame's
    current status first and skips the PATCH when it already matches —
    mirrors the `set_comment_completed` / Notion `set_oppgave_status`
    pattern.
    """
    if status not in {"active", "inactive"}:
        raise ValueError(
            f"set_project_status: invalid status {status!r} "
            "(expected 'active' or 'inactive')"
        )
    token = await frame_auth.get_access_token()
    body = {"data": {"status": status}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/accounts/{_account_id()}/projects/{project_id}",
                json=body,
            ),
            op_name=f"set_project_status({status})",
        )
        _raise_for_status(response)
        project = _unwrap(response.json())
        logger.info(
            "frame project %s status → %r", project_id, project.get("status")
        )
        return project


# ============================================================
# Writes — folder / file CRUD used by sync_frame
# ============================================================
#
# Endpoint paths come from Adobe's V4 reference. Verify the first call against
# a real response when wiring up; the file's top docstring says "don't guess
# from docs", and these are the first endpoints to touch on Goldbox's tenant.
# A 404 on a known-good id usually means the path-shape is slightly off (e.g.
# the endpoint expects the folder id alone vs scoped under accounts/{aid}/).


def _account_id() -> str:
    from gb_automations.config import settings

    if not settings.frame_account_id:
        raise RuntimeError(
            "FRAME_ACCOUNT_ID is not configured "
            "(run `python -m gb_automations.scripts.frame_oauth_bootstrap`)"
        )
    return settings.frame_account_id


def _unwrap(payload: Any) -> Any:
    """V4 responses sometimes wrap a single object in {data: ...} and
    sometimes return it at the top level. Centralize the unwrap so callers
    don't each guess."""
    if isinstance(payload, dict) and "data" in payload and len(payload) <= 3:
        return payload["data"]
    return payload


async def get_project(project_id: str) -> dict[str, Any]:
    """Fetch a Frame.io Project. Used by sync_frame for self-heal (a 404 here
    on a cached project id means the project was archived/deleted in Frame
    and the cache row must be evicted) and for fetching missing view_url /
    root_folder_id on adopted projects."""
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(f"/accounts/{_account_id()}/projects/{project_id}"),
            op_name="get_project",
        )
        _raise_for_status(response)
        return _unwrap(response.json())


async def get_folder(folder_id: str) -> dict[str, Any]:
    """Fetch one folder. sync_frame uses this for self-heal: a 404 here on a
    cached id means the folder was trashed in Frame and the cache row must be
    evicted (mirrors the Notion `page_is_live` pattern)."""
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(f"/accounts/{_account_id()}/folders/{folder_id}"),
            op_name="get_folder",
        )
        _raise_for_status(response)
        return _unwrap(response.json())


async def list_folder_children(folder_id: str) -> list[dict[str, Any]]:
    """List a folder's direct children. Used by `_ensure_discipline_folder`
    and the placeholder-file lookup to find an existing entity before creating
    a duplicate.

    Paginated to exhaustion: when adoption runs against a CLIENT'S already-
    populated project (turning the integration on mid-project), the root folder
    can hold many entries. If our discipline folder / placeholder match sits on
    a later page and we only read the first, we'd create a duplicate alongside
    their existing structure.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        return await _get_all_pages(
            client,
            f"/accounts/{_account_id()}/folders/{folder_id}/children",
            op_name="list_folder_children",
        )


async def create_folder(parent_folder_id: str, name: str) -> dict[str, Any]:
    """Create a sub-folder under `parent_folder_id`. Returns the new folder
    object (id + name + parent_id at minimum)."""
    token = await frame_auth.get_access_token()
    body = {"data": {"name": name}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.post(
                f"/accounts/{_account_id()}/folders/{parent_folder_id}/folders",
                json=body,
            ),
            op_name="create_folder",
        )
        _raise_for_status(response)
        folder = _unwrap(response.json())
        logger.info(
            "frame folder created %s under %s (id=%s)",
            name,
            parent_folder_id,
            folder.get("id"),
        )
        return folder


async def rename_folder(folder_id: str, new_name: str) -> dict[str, Any]:
    """Rename a folder in place. The folder id is preserved so cached
    child-folder/file ids underneath stay valid."""
    token = await frame_auth.get_access_token()
    body = {"data": {"name": new_name}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/accounts/{_account_id()}/folders/{folder_id}",
                json=body,
            ),
            op_name="rename_folder",
        )
        _raise_for_status(response)
        folder = _unwrap(response.json())
        logger.info("frame folder renamed %s → %s", folder_id, new_name)
        return folder


async def create_file_from_url(
    folder_id: str, name: str, source_url: str
) -> dict[str, Any]:
    """Create a File in `folder_id` by having Frame fetch the bytes from
    `source_url`. Used to seed the per-task placeholder asset; the source URL
    is served by our own FastAPI app over the Cloudflare tunnel.

    The endpoint and field shape were confirmed against the live V4 API on
    2026-05-26: `POST /accounts/{aid}/folders/{fid}/files/remote_upload` with
    body `{"data": {"name": ..., "source_url": ...}}`. The plain `/files`
    endpoint exists but rejects a `source` field as "Unexpected"; remote_upload
    is the documented path for "fetch from URL" creates and is not plan-gated
    on Goldbox's account.
    """
    token = await frame_auth.get_access_token()
    body = {"data": {"name": name, "source_url": source_url}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.post(
                f"/accounts/{_account_id()}/folders/{folder_id}/files/remote_upload",
                json=body,
            ),
            op_name="create_file_from_url",
        )
        _raise_for_status(response)
        file_obj = _unwrap(response.json())
        logger.info(
            "frame file created %s in folder %s (id=%s)",
            name,
            folder_id,
            file_obj.get("id"),
        )
        return file_obj


async def get_file(file_id: str) -> dict[str, Any]:
    """Fetch one File. Phase 1 doesn't read placeholder files back, but Phase 2
    will (comment polling joins back through file_id), so the read shape is
    here ahead of need."""
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(f"/accounts/{_account_id()}/files/{file_id}"),
            op_name="get_file",
        )
        _raise_for_status(response)
        return _unwrap(response.json())


async def delete_file(file_id: str) -> None:
    """Delete a Frame file. Used by the placeholder re-upload path when
    Frame's async background fetcher dropped the source-url fetch and
    left the file with empty media — we re-create the file from scratch
    instead of trying to PATCH bytes onto an existing slot (V4 doesn't
    expose that). 404 is treated as already-gone (idempotent)."""
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.delete(
                f"/accounts/{_account_id()}/files/{file_id}"
            ),
            op_name="delete_file",
        )
        if response.status_code == 404:
            logger.info("frame file %s already deleted", file_id)
            return
        _raise_for_status(response)
        logger.info("frame file %s deleted", file_id)


async def rename_file(file_id: str, new_name: str) -> dict[str, Any]:
    """Rename a file in place. Same `PATCH /accounts/{aid}/files/{id}` shape as
    `rename_folder` — Frame V4 keeps the file id stable, so its membership in a
    version stack and any cached references survive the rename. Used by the
    flattened Leveranse layout where the placeholder filename IS the visible
    label in Frame's UI (no wrapping folder)."""
    token = await frame_auth.get_access_token()
    body = {"data": {"name": new_name}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/accounts/{_account_id()}/files/{file_id}",
                json=body,
            ),
            op_name="rename_file",
        )
        _raise_for_status(response)
        file_obj = _unwrap(response.json())
        logger.info("frame file renamed %s → %s", file_id, new_name)
        return file_obj


async def list_version_stack_children(stack_id: str) -> list[dict[str, Any]]:
    """List the files inside a version stack, in Frame's stable ordering.

    Frame V4 endpoint: GET /accounts/{aid}/version_stacks/{sid}/children.
    Verified 2026-05-27 against a real V00→V01 stack: returns all File
    entities under the stack, each carrying `id`, `name`, `type`,
    `created_at`. `version_number` is present but null in observed
    responses — round derivation must sort by `created_at` ascending
    (V00 = oldest, V01 = next, etc.).

    DO NOT call `list_folder_children(stack_id)` for this — Frame V4
    distinguishes folders from version stacks at the URL level and
    returns 422 "Entity ... is not a folder" for a stack id under
    /folders/{id}/children.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/version_stacks/{stack_id}/children"
            ),
            op_name="list_version_stack_children",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


# ============================================================
# Phase 2 — comments + webhooks
# ============================================================
#
# Comment shape verified 2026-05-26 against Goldbox's Frame workspace
# (see docs/misc/frame-setup.md "probe phase"). Author + replies require
# `?include=owner,replies` (comma-separated — repeated keys silently drop
# all but the last). Replies appear nested under parent's `replies: [...]`
# and themselves carry no `parent_id`. Webhooks send
# `X-Frameio-Signature: v0=<hex>` with `v0:{ts}:{body}` HMAC-SHA256 over
# the raw body, ±5-minute replay window.


async def list_comments(
    file_id: str, *, include: tuple[str, ...] = ("owner", "replies")
) -> list[dict[str, Any]]:
    """List comments on a Frame File, with optional `?include=` expansions.

    V4 omits `owner` (the author) and `replies` (the nested reply chain)
    from the default response — they're privacy/perf gated and have to be
    opted into explicitly. The defaults here (`owner` + `replies`) match
    what the Phase 2 engine needs to render a Notion bullet with author
    attribution and nested reply bullets in one round-trip.

    Source: https://forum.frame.io/t/no-comment-replies-api-in-v4/2922
    plus the migration guide at next.developer.frame.io.

    Returns the raw `data` array. Each comment carries nested
    `owner: {id, name, email, active, adobe_user_id, avatar_url}` when the
    commenter is a registered Frame user — for external/guest commenters
    `owner` is `null` by Frame's privacy policy and will not be backfilled.
    Replies appear nested under their parent as `replies: [...]`; the
    reply object does NOT carry its own `parent_id`, so the engine derives
    "this is a reply" from where it sits in the response tree.

    Important: include params MUST be comma-separated in a single
    `?include=` key (verified 2026-05-26 against V4). Using repeated keys
    (`?include=owner&include=replies`) silently drops everything but the
    last value — replies came back but `owner` was null.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        params = {"include": ",".join(include)} if include else None
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/files/{file_id}/comments",
                params=params,
            ),
            op_name="list_comments",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


async def set_comment_completed(
    comment_id: str, completed: bool
) -> dict[str, Any]:
    """Mark a Frame comment as resolved (completed=True) or reopen it
    (completed=False). Returns the updated comment object.

    Endpoint shape verified 2026-05-27 against Goldbox's workspace:
    PATCH /accounts/{aid}/comments/{id} with body
    `{"data": {"completed": true|false}}` returns 200. On the response,
    `completed_at` and `completer_id` are populated when True, cleared
    when False.

    Used by Phase 2.5's Notion → Frame propagation (sync_oppgave_done):
    when the team toggles the Ferdig checkbox on a Korreksjon Oppgave
    in Notion, this PATCHes Frame so the comment's UI reflects the same
    state. Loop-prevention: the caller (`sync_oppgave_done`) reads
    Frame's current completed_at first and skips if already matching.
    """
    token = await frame_auth.get_access_token()
    body = {"data": {"completed": completed}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.patch(
                f"/accounts/{_account_id()}/comments/{comment_id}",
                json=body,
            ),
            op_name=f"set_comment_completed({completed})",
        )
        _raise_for_status(response)
        comment = _unwrap(response.json())
        logger.info(
            "frame comment %s set completed=%s (completer=%s)",
            comment_id,
            completed,
            comment.get("completer_id"),
        )
        return comment


async def get_comment(
    comment_id: str, *, include: tuple[str, ...] = ("owner", "replies")
) -> dict[str, Any]:
    """Fetch a single comment by id, with `?include=owner,replies` (comma-
    separated) by default so callers get author + nested replies in one
    round-trip.

    Returns the unwrapped comment object (handles both `{data: {...}}`
    and bare-object response shapes). See `list_comments` for the
    rationale on the include params and the repeated-vs-comma-separated
    gotcha.

    Raises FrameAPIError with 404 if the comment was deleted in Frame —
    the engine treats this as "mark done silently", same self-heal
    pattern as sync_frame uses for stale folder ids.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        params = {"include": ",".join(include)} if include else None
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/comments/{comment_id}",
                params=params,
            ),
            op_name="get_comment",
        )
        _raise_for_status(response)
        return _unwrap(response.json())


# Backwards-compat alias for the probe-phase debug endpoint that returns
# the un-unwrapped JSON. Remove once /debug/frame/comment/{id} is taken
# out (the probe is done; we keep the debug endpoint as an operator tool).
async def get_comment_raw(
    comment_id: str, *, include: tuple[str, ...] = ("owner", "replies")
) -> dict[str, Any]:
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        params = {"include": ",".join(include)} if include else None
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/comments/{comment_id}",
                params=params,
            ),
            op_name="get_comment_raw",
        )
        _raise_for_status(response)
        return response.json()


async def list_webhooks(workspace_id: str) -> list[dict[str, Any]]:
    """List webhooks registered against a workspace.

    V4 endpoint: GET /accounts/{aid}/workspaces/{wid}/webhooks. Used by
    the bootstrap script to check whether `{PUBLIC_HUB}/webhooks/frame`
    is already registered before creating a duplicate, and by the
    /debug/frame/webhooks endpoint for visibility.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/workspaces/{workspace_id}/webhooks"
            ),
            op_name="list_webhooks",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


async def create_webhook(
    workspace_id: str,
    *,
    url: str,
    events: list[str],
    name: str,
) -> dict[str, Any]:
    """Register a new webhook on a workspace. Returns the new webhook
    object — the response carries a `secret` field that must be saved
    (it's never shown again) and used to verify HMAC signatures on
    incoming requests.

    V4 endpoint: POST /accounts/{aid}/workspaces/{wid}/webhooks. Body
    shape (verified via Adobe webhook docs):
        {"data": {"name": ..., "url": ..., "events": [...]}}

    Events for Phase 2 comments: `comment.created`, `comment.updated`,
    `comment.completed`, `comment.uncompleted`, `comment.deleted`.
    `comment.created` fires for replies too (there's no
    `comment.replied`); the engine detects "reply" by walking the parent
    comment's `replies: [...]` array, not the event type.
    """
    token = await frame_auth.get_access_token()
    body = {"data": {"name": name, "url": url, "events": events}}
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.post(
                f"/accounts/{_account_id()}/workspaces/{workspace_id}/webhooks",
                json=body,
            ),
            op_name="create_webhook",
        )
        _raise_for_status(response)
        webhook = _unwrap(response.json())
        logger.info(
            "frame webhook created %r → %s (id=%s, events=%s)",
            name,
            url,
            webhook.get("id"),
            events,
        )
        return webhook


async def delete_webhook(webhook_id: str) -> None:
    """Delete a webhook by id. Used by the bootstrap script + manual
    operator cleanup to remove stale registrations.

    V4 endpoint: DELETE /accounts/{aid}/webhooks/{wid}. Idempotent: a
    404 is logged but not raised, so a webhook that was already deleted
    out-of-band doesn't crash a cleanup pass.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.delete(
                f"/accounts/{_account_id()}/webhooks/{webhook_id}"
            ),
            op_name="delete_webhook",
        )
        if response.status_code == 404:
            logger.info(
                "frame webhook %s already gone — delete is a no-op", webhook_id
            )
            return
        _raise_for_status(response)
        logger.info("frame webhook %s deleted", webhook_id)


async def get_file_version_info(file_id: str) -> tuple[int, str | None]:
    """Derive the round number for a Frame File from its version stack.

    Returns `(round_number, version_stack_id)`:
      - V00 placeholder, never had a real version uploaded on top of it
        → (0, None). The file's parent is the task folder, not a stack.
      - V00 inside a version stack (a V01 was uploaded on top of it,
        Frame wrapped both in a stack) → (0, <stack_id>).
      - V01 (first real delivery) → (1, <stack_id>).
      - V02 (revision after round 1) → (2, <stack_id>). Etc.

    Verified 2026-05-27 against a real V00 → V01 stack on Goldbox's
    workspace:
      - A file's `parent_id` is the version_stack id when versions exist;
        the same `parent_id` field holds the task folder id when the file
        is bare. The two are distinguished by GET-ing them: a folder
        responds 200 at /folders/{id}, a stack 422 (and vice versa).
      - The version stack's /children endpoint is
        /accounts/{aid}/version_stacks/{sid}/children — NOT the /folders
        equivalent (which 422s on a stack id).
      - Each child carries `created_at`. `version_number` is present but
        null in observed responses, so we derive round = index when
        children are sorted by `created_at` ascending (V00 = oldest).
    """
    file_obj = await get_file(file_id)
    parent_id = file_obj.get("parent_id")
    if not parent_id:
        logger.warning(
            "frame file %s has no parent_id — assuming round 0",
            file_id,
        )
        return 0, None

    # Try the version-stack endpoint. If it 422s the parent is a folder
    # (bare V00, no real delivery on top) → round 0, no stack.
    try:
        stack_children = await list_version_stack_children(parent_id)
    except FrameAPIError as err:
        if err.status_code in (404, 422):
            logger.info(
                "frame file %s parent %s is not a version_stack — bare V00, round 0",
                file_id, parent_id,
            )
            return 0, None
        raise

    # Sort oldest-first. Frame's UI orders V00, V01, V02 by upload time,
    # and `created_at` is what backs that ordering.
    stack_children.sort(key=lambda c: c.get("created_at", ""))
    for idx, child in enumerate(stack_children):
        if child.get("id") == file_id:
            logger.info(
                "frame file %s is index %d in stack %s → round %d",
                file_id, idx, parent_id, idx,
            )
            return idx, parent_id

    # Shouldn't happen: the file's parent is the stack but the file
    # isn't in the stack's children list. Fall back to round 0 with a
    # loud warning so we notice if the shape ever drifts.
    logger.warning(
        "frame file %s not found in stack %s children — assuming round 0",
        file_id, parent_id,
    )
    return 0, parent_id


# ============================================================
# Custom metadata fields (V4 — "Status" custom select is the use case)
# ============================================================
#
# Frame V4 stores user-defined select / multi-select / text / etc. fields
# as workspace-level **field definitions** referenced by UUID. The
# canonical write endpoint runs through the project (not the file):
#
#   POST /v4/accounts/{aid}/projects/{pid}/metadata/values
#     body: {"data": {"file_ids": [fid], "values": [
#       {"field_definition_id": uuid, "value": "Utgår"}
#     ]}}
#
# Writing through the file endpoint directly returns 422; this is a known
# gotcha confirmed via the Frame developer forum
# (https://forum.frame.io/t/.../2996).
#
# Read shape on a File response: the custom field values come back under
# a `metadata` array — each entry carries `field_definition_id`,
# `field_name`, `field_type`, and `value` (or `value_id` for selects).
# Probed against Goldbox's tenant during implementation; the helper
# below is defensive to absorb shape drift.


async def list_field_definitions() -> list[dict[str, Any]]:
    """List the workspace's custom field definitions.

    Operator-facing: backs `/debug/frame/field-definitions` so the
    Status field's UUID can be discovered without curl. Each entry
    typically carries `id`, `name`, `field_type` (e.g. "select"), and
    for selects an `options` array with `{id, value}` per option.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/metadata/field_definitions"
            ),
            op_name="list_field_definitions",
        )
        _raise_for_status(response)
        payload = response.json()
        # V4 returns either {"data": [...]} or a bare list depending on
        # the endpoint. Normalize.
        if isinstance(payload, dict) and isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload, list):
            return payload
        return []


# Cached option maps for select fields. Frame V4 select-field reads
# and writes use option UUIDs (NOT the display name), so we need to
# translate {"Utgår" ↔ <option-uuid>} both directions. The map only
# changes when an admin adds/removes options in the Frame UI — process-
# local cache is fine; a restart re-warms it. Cleared via
# `_invalidate_select_options_cache(field_id)` if we ever need a
# manual refresh.
_SELECT_OPTIONS_CACHE: dict[str, dict[str, str]] = {}  # field_id → {display_name: uuid}


async def _select_options_for_field(field_id: str) -> dict[str, str]:
    """Return `{display_name: option_uuid}` for a select field.

    Hot path called on every read AND write of a select-field value.
    First miss costs one `GET /metadata/field_definitions` round-trip;
    subsequent calls are O(1) in-memory.
    """
    cached = _SELECT_OPTIONS_CACHE.get(field_id)
    if cached is not None:
        return cached
    definitions = await list_field_definitions()
    for definition in definitions:
        if not isinstance(definition, dict):
            continue
        if definition.get("id") != field_id:
            continue
        config = definition.get("field_configuration") or {}
        options = config.get("options") or []
        mapping: dict[str, str] = {}
        for opt in options:
            if not isinstance(opt, dict):
                continue
            name = opt.get("display_name")
            uuid = opt.get("id")
            if isinstance(name, str) and isinstance(uuid, str):
                mapping[name] = uuid
        _SELECT_OPTIONS_CACHE[field_id] = mapping
        return mapping
    # Field not found at all — cache empty mapping so we don't re-fetch
    # every call. Operator should fix FRAME_STATUS_FIELD_ID.
    _SELECT_OPTIONS_CACHE[field_id] = {}
    return {}


def _invalidate_select_options_cache(field_id: str | None = None) -> None:
    """Clear the option-name → UUID cache. Pass a field_id to evict one
    field, or None to evict all. Used after an admin adds a new option
    in Frame's UI and we need to pick it up without restarting."""
    if field_id is None:
        _SELECT_OPTIONS_CACHE.clear()
    else:
        _SELECT_OPTIONS_CACHE.pop(field_id, None)


def _extract_status_value_uuid(file_payload: dict[str, Any], field_id: str) -> str | None:
    """Pull the current Status option UUID out of a `GET /files/{id}` payload.

    Frame V4 stores select-field values as option UUIDs (NOT display
    names). The file payload's `metadata` array entries carry `value`
    as either a list[uuid] (the verified shape for selects) or a bare
    string in some payload variants. Returns the FIRST option UUID, or
    None for "field unset" / any unmatched shape.
    """
    if not field_id:
        return None
    md = file_payload.get("metadata") if isinstance(file_payload, dict) else None
    if not isinstance(md, list):
        return None
    for entry in md:
        if not isinstance(entry, dict):
            continue
        if entry.get("field_definition_id") != field_id:
            continue
        val = entry.get("value")
        if isinstance(val, list):
            # Select fields surface as a list of option UUIDs.
            return val[0] if val else None
        if isinstance(val, str):
            return val or None
        return None
    return None


async def get_file_status(file_id: str, field_id: str) -> str | None:
    """Read the current value of the workspace's custom Status field on
    a File, returned as a DISPLAY NAME (e.g. "Utgår", "Needs Review").

    Frame stores the underlying value as an option UUID; this helper
    resolves it back to the display name via the cached options map so
    callers can compare against `FRAME_STATUS_UTGAAR` directly. Returns
    None if the field is unset, the file doesn't carry the field, the
    stored UUID doesn't match any current option (renamed/deleted), or
    `field_id` is empty.
    """
    if not field_id:
        return None
    file_obj = await get_file(file_id)
    uuid = _extract_status_value_uuid(file_obj, field_id)
    if uuid is None:
        return None
    # Reverse-map UUID → display name.
    options = await _select_options_for_field(field_id)
    for name, opt_uuid in options.items():
        if opt_uuid == uuid:
            return name
    return None  # stored UUID doesn't match any current option — treat as unset


async def set_file_status(
    *, project_id: str, file_id: str, field_id: str, value: str | None
) -> str:
    """Write (or clear) the workspace Status custom field on a File.

    Returns the action taken: "written" | "unchanged" | "skipped".

    - `value` is the option's DISPLAY NAME (e.g. "Utgår"). This helper
      resolves it to the option UUID before writing (Frame V4 select
      fields are wire-typed as `List of UUID`, not strings).
    - `value=None` clears the field — sent as an empty list.
    - `field_id` empty → returns "skipped" without an HTTP call (sync
      disabled via FRAME_STATUS_FIELD_ID env var).
    - Read-first: fetches the current display-name via `get_file_status`
      and short-circuits with "unchanged" when Frame already matches.
      This is the loop-prevention guard.
    """
    if not field_id:
        return "skipped"
    current = await get_file_status(file_id, field_id)
    if current == value:
        return "unchanged"

    # Resolve desired display name → option UUID(s).
    if value is None:
        option_uuids: list[str] = []
    else:
        options = await _select_options_for_field(field_id)
        if value not in options:
            logger.warning(
                "frame file %s status: option %r not found among %s "
                "(check Frame field config / cache freshness)",
                file_id,
                value,
                sorted(options.keys()),
            )
            return "skipped"
        option_uuids = [options[value]]

    body: dict[str, Any] = {
        "data": {
            "file_ids": [file_id],
            "values": [
                {
                    "field_definition_id": field_id,
                    # Frame V4 wants a List of UUID for select fields,
                    # not the display-name string. Verified via the
                    # probe endpoint's 422: "A List of UUID is expected
                    # for select field type".
                    "value": option_uuids,
                }
            ],
        }
    }
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            # Custom-fields metadata writes live under the experimental
            # router with PATCH (not POST — POST returns 404 even with
            # the experimental header). The endpoint is project-scoped:
            # PATCH /v4/accounts/{aid}/projects/{pid}/metadata/values
            lambda: client.patch(
                f"/accounts/{_account_id()}/projects/{project_id}/metadata/values",
                json=body,
                headers={"api-version": "experimental"},
            ),
            op_name="set_file_status",
        )
        _raise_for_status(response)
    logger.info(
        "frame file %s status: %r → %r (project=%s, field=%s, option_uuids=%s)",
        file_id,
        current,
        value,
        project_id,
        field_id,
        option_uuids,
    )
    return "written"
