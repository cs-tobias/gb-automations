"""OAuth callback route for the Frame.io bootstrap flow.

Why it lives in the live FastAPI rather than a one-shot local server:
Adobe IMS requires HTTPS for every redirect URI, including localhost. Our
Cloudflare Tunnel (`hub.{domain}`) already terminates TLS for the existing
webhooks, so reusing it for the OAuth callback avoids the self-signed-cert
detour — at the cost of putting an endpoint into the prod service that's
only meaningful during the one-time bootstrap.

Coordination with the CLI:
  1. The bootstrap CLI generates a random state token, writes it to
     `/tmp/frame_oauth_state.txt`, and prints the consent URL.
  2. You sign in at Adobe; Adobe redirects to /oauth/frame/callback?code=...&state=...
  3. This handler verifies the state file matches, exchanges the code for
     tokens, and writes them to `/tmp/frame_oauth_result.json` (with the
     refresh token).
  4. The CLI polls that file, reads the tokens, runs the account-picker,
     and prints the values you should put in .env.

`/tmp` is the api container's writable layer — both this handler and the
CLI run inside the same container (via `docker compose exec api`), so the
files are visible to both processes without needing an extra mount.

Security: the state token must be a fresh random per-bootstrap value AND
the handler refuses to operate when the state file is missing — so even
if someone hits /oauth/frame/callback at a random time in production, no
exchange happens. Once the bootstrap completes, the state file is deleted.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from gb_automations.clients import frame_auth
from gb_automations.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


# In-container paths shared with scripts/frame_oauth_bootstrap.py.
# /tmp is writable in the api image's runtime layer.
STATE_PATH = Path("/tmp/frame_oauth_state.txt")
RESULT_PATH = Path("/tmp/frame_oauth_result.json")


@router.get("/oauth/frame/callback")
async def frame_callback(request: Request) -> HTMLResponse:
    """Adobe IMS redirects here after the user consents.

    Returns a small HTML page so the user sees "you can close this tab" in
    the browser — the actual handoff to the CLI is the JSON file we write.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    error = request.query_params.get("error")
    error_description = request.query_params.get("error_description")

    if error:
        logger.warning("Frame.io OAuth callback got error=%s desc=%s", error, error_description)
        return _page(
            "Authorization failed",
            f"<p>Adobe IMS returned an error: <code>{error}</code></p>"
            f"<p>{error_description or ''}</p>"
            "<p>Re-run the bootstrap script and try again.</p>",
            status=400,
        )

    if not code or not state:
        return _page(
            "Missing parameters",
            "<p>This URL is only meaningful as a redirect from Adobe IMS during "
            "the one-time Frame.io OAuth bootstrap. If you reached it directly, "
            "you can close this tab.</p>",
            status=400,
        )

    if not STATE_PATH.exists():
        logger.warning("Frame.io OAuth callback fired but no bootstrap in progress")
        return _page(
            "No bootstrap in progress",
            "<p>No active bootstrap was found. Run "
            "<code>docker compose exec api python -m "
            "gb_automations.scripts.frame_oauth_bootstrap</code> first.</p>",
            status=400,
        )

    expected_state = STATE_PATH.read_text(encoding="utf-8").strip()
    if state != expected_state:
        logger.warning("Frame.io OAuth state mismatch (CSRF protection triggered)")
        return _page(
            "State mismatch",
            "<p>The state token didn't match the in-progress bootstrap. "
            "This is a CSRF protection trigger — re-run the bootstrap script.</p>",
            status=400,
        )

    if not settings.frame_redirect_uri:
        return _page(
            "FRAME_REDIRECT_URI not set",
            "<p>Set <code>FRAME_REDIRECT_URI</code> in <code>.env</code> and restart "
            "the API container before bootstrapping.</p>",
            status=500,
        )

    try:
        tokens = await frame_auth.exchange_code(
            code, redirect_uri=settings.frame_redirect_uri
        )
    except frame_auth.FrameAuthError as err:
        logger.exception("Frame.io OAuth code exchange failed")
        return _page("Token exchange failed", f"<pre>{err}</pre>", status=500)

    # Persist the result for the CLI to pick up. Refresh token is the
    # operationally critical value — without it, every restart needs a new
    # OAuth dance.
    if not tokens.get("refresh_token"):
        return _page(
            "No refresh token returned",
            "<p>Adobe returned access_token but no refresh_token. The "
            "<code>offline_access</code> scope is probably missing from the "
            "Adobe Developer Console credential — add it and re-run.</p>",
            status=500,
        )

    RESULT_PATH.write_text(json.dumps(tokens), encoding="utf-8")
    # State is single-use; remove so a stray /callback re-hit can't re-trigger.
    STATE_PATH.unlink(missing_ok=True)

    logger.info(
        "Frame.io OAuth bootstrap complete — refresh token written to %s",
        RESULT_PATH,
    )
    return _page(
        "Authorization complete",
        "<p>You can close this tab. The bootstrap script will now print the "
        "values to put in <code>.env</code>.</p>",
    )


def _page(title: str, body_html: str, *, status: int = 200) -> HTMLResponse:
    html = (
        "<!doctype html><meta charset=utf-8>"
        f"<title>{title}</title>"
        "<style>body{font:14px/1.5 system-ui,sans-serif;max-width:560px;"
        "margin:60px auto;padding:0 20px;color:#333}"
        "code{background:#f4f4f4;padding:2px 6px;border-radius:3px}"
        "pre{background:#f4f4f4;padding:10px;border-radius:3px;overflow:auto}</style>"
        f"<h1>{title}</h1>{body_html}"
    )
    return HTMLResponse(html, status_code=status)
