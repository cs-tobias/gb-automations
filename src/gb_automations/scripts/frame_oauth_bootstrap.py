"""One-time Frame.io OAuth bootstrap.

Goldbox runs Frame.io on a non-Enterprise plan, so the only path to
unattended backend access is OAuth Web App + offline_access scope. This
script handles the one human-in-the-loop step needed to get a refresh
token. Re-run it only if the refresh token is ever invalidated (Adobe
password change, credential rotation).

Flow (run inside the api container):

    docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap

  1. Generates a CSRF state token, writes it to /tmp/frame_oauth_state.txt.
  2. Prints the Adobe IMS consent URL. You open it in your browser and
     sign in as the shared Goldbox Frame.io account (petter@goldbox.no).
  3. Adobe redirects to FRAME_REDIRECT_URI (which must point at the live
     FastAPI's /oauth/frame/callback over the Cloudflare tunnel).
  4. The callback handler exchanges the code for tokens and writes them
     to /tmp/frame_oauth_result.json (visible to this script because we
     share the container's /tmp).
  5. This script polls that file, reads the refresh token, calls /me +
     /accounts + /accounts/{id}/workspaces to confirm the token works and
     to let you pick the right workspace.
  6. Prints the lines to paste into .env. (We don't write .env directly
     because the file lives on the host, not in the container — copy-
     paste is more transparent than a host-mount round-trip.)
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

import httpx

from gb_automations.clients import frame, frame_auth
from gb_automations.config import settings
from gb_automations.routes.oauth import RESULT_PATH, STATE_PATH


_POLL_INTERVAL_S = 2.0
_TIMEOUT_S = 600.0  # 10 min for the human-in-the-loop step


async def main() -> None:
    if not settings.frame_client_id or not settings.frame_client_secret:
        _die(
            "FRAME_CLIENT_ID and FRAME_CLIENT_SECRET must be set in .env "
            "before running the bootstrap.\n"
            "  Get them from: https://developer.adobe.com/console → your "
            "project → Credentials → OAuth Web App"
        )
    if not settings.frame_redirect_uri:
        _die(
            "FRAME_REDIRECT_URI must be set in .env. Example:\n"
            "  FRAME_REDIRECT_URI=https://hub.tobiaseek.com/oauth/frame/callback\n"
            "(Must match EXACTLY the Redirect URI you set in Adobe Developer Console.)"
        )

    # Clear any leftover state from a prior aborted run.
    STATE_PATH.unlink(missing_ok=True)
    RESULT_PATH.unlink(missing_ok=True)

    state = secrets.token_urlsafe(32)
    STATE_PATH.write_text(state, encoding="utf-8")

    authorize_url = frame_auth.build_authorize_url(
        redirect_uri=settings.frame_redirect_uri,
        state=state,
    )

    print()
    print("=" * 76)
    print("Frame.io OAuth bootstrap")
    print("=" * 76)
    print()
    print("Step 1 — Open this URL in your browser and sign in as the shared")
    print("         Goldbox Frame.io account (petter@goldbox.no):")
    print()
    print(f"  {authorize_url}")
    print()
    print("Step 2 — After Adobe redirects back, this script will pick up the")
    print("         tokens automatically. Don't close this terminal.")
    print()
    print(f"Waiting for callback (timeout {int(_TIMEOUT_S)}s)…")

    tokens = await _wait_for_result()
    if tokens is None:
        STATE_PATH.unlink(missing_ok=True)
        _die("Timed out waiting for the OAuth callback. Re-run when ready.")

    # Single-use file — clean up immediately.
    RESULT_PATH.unlink(missing_ok=True)

    refresh_token = tokens.get("refresh_token", "")
    access_token = tokens.get("access_token", "")
    if not refresh_token or not access_token:
        _die(
            "Adobe returned an incomplete token response. Confirm that the "
            "`offline_access` scope is enabled on the Adobe Developer Console "
            "credential."
        )

    # Stash the refresh token so frame_auth.get_access_token() works during
    # this script's remaining calls — without it, the account/workspace
    # picker would refuse to call Frame.io.
    settings.frame_refresh_token = refresh_token
    frame_auth.reset_cache()

    print()
    print("✓ Got refresh + access tokens.")
    print()
    print("Step 3 — Confirming the token works (GET /v4/me)…")

    try:
        me = await frame.whoami()
    except httpx.HTTPError as err:
        _die(f"Whoami call failed: {err}")
    except frame.FrameAPIError as err:
        _die(str(err))

    me_email = (me.get("data") or me).get("email") or "(unknown)"
    me_name = (me.get("data") or me).get("name") or "(unknown)"
    print(f"  Authenticated as: {me_name} <{me_email}>")
    if "petter@goldbox.no" not in me_email.lower():
        print()
        print(
            f"  ⚠  Heads-up: signed in as {me_email}, not petter@goldbox.no. "
            "If that's not the shared studio account, abort and re-run "
            "after signing into the right Adobe ID."
        )

    print()
    print("Step 4 — Picking account + workspace…")

    accounts = await frame.list_accounts()
    if not accounts:
        _die("This user has no Frame.io accounts. Check the sign-in.")
    account = _pick("account", accounts)

    workspaces = await frame.list_workspaces(account["id"])
    if not workspaces:
        _die(f"Account {account['name']!r} has no workspaces.")
    workspace = _pick("workspace", workspaces)

    print()
    print("=" * 76)
    print("DONE — paste these into .env (host machine), then restart the api:")
    print("=" * 76)
    print()
    print(f"FRAME_REFRESH_TOKEN={refresh_token}")
    print(f"FRAME_ACCOUNT_ID={account['id']}")
    print(f"FRAME_WORKSPACE_ID={workspace['id']}")
    print()
    print("Then on the host:")
    print("  docker compose up -d --force-recreate api")
    print()
    print(
        "Test with: curl https://hub.{your-domain}/debug/frame  "
        "→ should return {\"ok\": true, ...}"
    )
    print()


async def _wait_for_result() -> dict | None:
    """Poll for the callback handler to write tokens to /tmp."""
    deadline = time.monotonic() + _TIMEOUT_S
    while time.monotonic() < deadline:
        if RESULT_PATH.exists():
            try:
                return json.loads(RESULT_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # File mid-write — try again next tick.
                pass
        await asyncio.sleep(_POLL_INTERVAL_S)
    return None


def _pick(label: str, items: list[dict]) -> dict:
    """Interactive picker. Auto-selects when there's only one option."""
    if len(items) == 1:
        only = items[0]
        print(f"  Single {label} found: {only.get('name')!r} ({only['id']}) — using it.")
        return only

    print()
    print(f"  Multiple {label}s — pick one:")
    for i, item in enumerate(items, start=1):
        print(f"    {i}. {item.get('name', '(no name)')} — {item['id']}")
    while True:
        raw = input(f"  Enter number (1-{len(items)}): ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
