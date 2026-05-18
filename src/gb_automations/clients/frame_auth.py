"""Frame.io V4 OAuth token management.

Goldbox runs Frame.io on a non-Enterprise plan, which means Adobe's clean
Server-to-Server credential type is unavailable. We use OAuth Web App with
the `offline_access` scope to get a long-lived refresh token at bootstrap
time, then exchange it for short-lived access tokens on demand.

Adobe IMS access tokens currently last 24h. We cache the current access
token in-process with a small safety margin and refresh on demand — there's
no scheduled refresh loop because most workers will sit idle most of the
time, and forcing wake-ups just to keep a token warm wastes both API quota
and electricity.

The refresh token itself doesn't expire under the offline_access scope, but
it CAN be invalidated by:
  - Rotating the API credential in Adobe Developer Console.
  - The user (petter@goldbox.no) changing their Adobe password.
  - Adobe security action.
When that happens, every refresh attempt returns 400 invalid_grant — the
caller should surface that loudly and instruct re-running the bootstrap.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from gb_automations.config import settings

logger = logging.getLogger(__name__)


# Adobe IMS NA1 endpoints. Frame.io is currently NA1-only regardless of where
# the workspace lives geographically — confirmed in the V4 Getting Started
# guide. If Adobe ever surfaces an EU endpoint, switch on a setting.
_IMS_TOKEN_URL = "https://ims-na1.adobelogin.com/ims/token/v3"
_IMS_AUTHORIZE_URL = "https://ims-na1.adobelogin.com/ims/authorize/v2"

# Scopes required for unattended backend access. `offline_access` is the
# critical one — without it, Adobe returns no refresh_token in the auth
# exchange and every access token dies after 24h with no way to renew.
# `openid`, `profile`, `email` are the standard OIDC trio Adobe expects.
# `additional_info.roles` is Frame.io-specific — surfaces the user's role
# in each workspace they belong to, useful for the bootstrap account picker.
SCOPES = ("openid", "profile", "email", "offline_access", "additional_info.roles")


# Refresh slightly before the token actually expires so an in-flight request
# never races a mid-call expiry. 60s is plenty given IMS round-trip is ~200ms.
_REFRESH_SAFETY_MARGIN_S = 60.0


class FrameAuthError(RuntimeError):
    """Auth failure that the user must resolve manually.

    Distinct from transient HTTP errors — callers should NOT retry on this;
    they should surface the message and let the operator re-run the bootstrap
    script.
    """


_token_lock = asyncio.Lock()
_cached_access_token: str | None = None
_cached_access_token_expires_at: float = 0.0


async def get_access_token(*, force_refresh: bool = False) -> str:
    """Return a currently-valid Frame.io API access token.

    Caches in-process across calls. Concurrent callers serialize on a single
    refresh — Adobe IMS is fine with rapid back-to-back refreshes but we'd
    burn quota and risk minor inconsistency if N workers all refreshed at
    once on cold start.

    Raises `FrameAuthError` if the refresh token is missing or rejected.
    """
    global _cached_access_token, _cached_access_token_expires_at

    now = time.monotonic()
    if (
        not force_refresh
        and _cached_access_token
        and now < _cached_access_token_expires_at - _REFRESH_SAFETY_MARGIN_S
    ):
        return _cached_access_token

    async with _token_lock:
        # Re-check after acquiring the lock — another coroutine may have
        # refreshed while we were waiting.
        now = time.monotonic()
        if (
            not force_refresh
            and _cached_access_token
            and now < _cached_access_token_expires_at - _REFRESH_SAFETY_MARGIN_S
        ):
            return _cached_access_token

        if not settings.frame_refresh_token:
            raise FrameAuthError(
                "FRAME_REFRESH_TOKEN is empty. Run "
                "`python -m gb_automations.scripts.frame_oauth_bootstrap` "
                "to authorize the integration."
            )
        if not settings.frame_client_id or not settings.frame_client_secret:
            raise FrameAuthError(
                "FRAME_CLIENT_ID and FRAME_CLIENT_SECRET must be set "
                "(Adobe Developer Console → your project → Credentials)."
            )

        token, expires_in = await _refresh(settings.frame_refresh_token)
        _cached_access_token = token
        _cached_access_token_expires_at = time.monotonic() + expires_in
        logger.info("Frame.io access token refreshed; valid for %ds", int(expires_in))
        return token


async def exchange_code(code: str, *, redirect_uri: str) -> dict:
    """One-time exchange of an auth code for tokens. Used by the bootstrap flow.

    Returns the raw token response dict so the bootstrap can inspect both
    `access_token` (for the immediate account-picker call) and `refresh_token`
    (which it persists to .env).
    """
    payload = {
        "grant_type": "authorization_code",
        "client_id": settings.frame_client_id,
        "client_secret": settings.frame_client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(_IMS_TOKEN_URL, data=payload)
    if response.status_code >= 400:
        raise FrameAuthError(
            f"Adobe IMS code exchange failed ({response.status_code}): {response.text}"
        )
    return response.json()


async def _refresh(refresh_token: str) -> tuple[str, float]:
    """Exchange a refresh token for a fresh access token. Returns (token, expires_in_s)."""
    payload = {
        "grant_type": "refresh_token",
        "client_id": settings.frame_client_id,
        "client_secret": settings.frame_client_secret,
        "refresh_token": refresh_token,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(_IMS_TOKEN_URL, data=payload)
    if response.status_code >= 400:
        # Adobe returns 400 invalid_grant when the refresh token is dead.
        # Surface explicitly because the fix is operational, not retryable.
        raise FrameAuthError(
            f"Adobe IMS refresh failed ({response.status_code}): {response.text}. "
            "If this says invalid_grant, the refresh token was revoked — "
            "re-run `python -m gb_automations.scripts.frame_oauth_bootstrap`."
        )
    body = response.json()
    return body["access_token"], float(body.get("expires_in", 86400))


def build_authorize_url(*, redirect_uri: str, state: str) -> str:
    """Construct the URL the user opens in their browser to start OAuth.

    `state` is a CSRF token the caller generates and verifies on the
    /oauth/frame/callback side. Adobe round-trips it untouched.
    """
    from urllib.parse import urlencode

    params = {
        "client_id": settings.frame_client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return f"{_IMS_AUTHORIZE_URL}?{urlencode(params)}"


def reset_cache() -> None:
    """Drop the in-process access-token cache. Useful for tests."""
    global _cached_access_token, _cached_access_token_expires_at
    _cached_access_token = None
    _cached_access_token_expires_at = 0.0
