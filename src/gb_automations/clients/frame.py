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

    Called from the bootstrap script to let the operator pick which Project
    will host all Goldbox folders (FRAME_ROOT_PROJECT_ID). Returns the raw
    `data` array — each item has at least `id` and `name`.
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
    """Fetch a Frame.io Project. Used by /debug/frame/project to confirm
    FRAME_ROOT_PROJECT_ID resolves on Goldbox's tenant."""
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
