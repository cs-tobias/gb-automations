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
    Returns the raw `data` array — each item has at least `id` and `name`,
    plus typically `root_folder_id` and `view_url`.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{account_id}/workspaces/{workspace_id}/projects"
            ),
            op_name="list_projects",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


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
    """List a folder's direct children. Used by `_ensure_discipline_folder` to
    find an existing discipline folder before creating a duplicate.

    No pagination cursor passed for now: a Goldbox project never has more
    than a handful of discipline folders. Add ?after=... if a real project
    ever blows past the default page size.
    """
    token = await frame_auth.get_access_token()
    async with await _client(access_token=token) as client:
        response = await _with_retries(
            lambda: client.get(
                f"/accounts/{_account_id()}/folders/{folder_id}/children"
            ),
            op_name="list_folder_children",
        )
        _raise_for_status(response)
        return response.json().get("data", [])


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
