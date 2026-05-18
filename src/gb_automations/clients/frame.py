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
