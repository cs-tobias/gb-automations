"""Register the Phase 2 Frame.io comment webhook.

Standalone from the OAuth bootstrap — uses the existing refresh token
to call POST /v4/.../webhooks, so you don't have to re-do the Adobe
consent dance just to (re-)create a webhook. The signing secret Frame
returns is captured and printed for pasting into .env as
FRAME_WEBHOOK_SECRET.

Run on the host (or inside the api container):

    docker compose exec api python -m gb_automations.scripts.frame_register_webhook

Idempotent in the "didn't already exist" direction:
  - If no webhook exists for our receiver URL: creates one, prints the secret.
  - If a webhook for our URL already exists AND FRAME_WEBHOOK_SECRET is
    set in the env: leaves it alone, exits 0.
  - If a webhook exists but FRAME_WEBHOOK_SECRET is unset: deletes the
    existing webhook, recreates it, prints the fresh secret. (Frame only
    returns the secret at create time; we can't fetch it after the fact.)

The receiver URL is derived from FRAME_REDIRECT_URI's hub origin — set it
once and both the OAuth callback and the webhook receiver scope to the
same Cloudflare tunnel.
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlparse, urlunparse

from gb_automations.clients import frame
from gb_automations.config import settings


_WEBHOOK_NAME = "gb-automations comment sync"
_WEBHOOK_EVENTS = [
    "comment.created",
    "comment.updated",
    "comment.completed",
    "comment.uncompleted",
    "comment.deleted",
]


def _receiver_url() -> str | None:
    redirect = settings.frame_redirect_uri
    if not redirect:
        return None
    parsed = urlparse(redirect)
    if not parsed.scheme or not parsed.netloc:
        return None
    return urlunparse(
        (parsed.scheme, parsed.netloc, "/webhooks/frame", "", "", "")
    )


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


async def main() -> None:
    if not settings.frame_account_id or not settings.frame_workspace_id:
        _die(
            "FRAME_ACCOUNT_ID and FRAME_WORKSPACE_ID must be set "
            "(run the OAuth bootstrap first)."
        )
    if not settings.frame_refresh_token:
        _die(
            "FRAME_REFRESH_TOKEN must be set "
            "(run the OAuth bootstrap first)."
        )

    receiver_url = _receiver_url()
    if not receiver_url:
        _die(
            "FRAME_REDIRECT_URI must be set so we can derive the "
            "webhook URL from its hub origin."
        )

    print()
    print(f"Receiver URL:  {receiver_url}")
    print(f"Workspace:     {settings.frame_workspace_id}")
    print(f"Events:        {', '.join(_WEBHOOK_EVENTS)}")
    print()

    try:
        existing = await frame.list_webhooks(settings.frame_workspace_id)
    except frame.FrameAPIError as err:
        _die(f"Could not list workspace webhooks: {err}")

    matching = [w for w in existing if w.get("url") == receiver_url]
    if matching:
        hook = matching[0]
        print(f"Existing webhook found (id={hook.get('id')}).")
        if settings.frame_webhook_secret:
            print(
                "FRAME_WEBHOOK_SECRET is already set — leaving the existing "
                "webhook in place.\n"
                "If you need a fresh secret, unset FRAME_WEBHOOK_SECRET in "
                ".env and re-run."
            )
            return
        print(
            "FRAME_WEBHOOK_SECRET is unset, so we can't reuse this hook "
            "(Frame only returns the secret at create time).\n"
            "Deleting and recreating to capture a fresh secret…"
        )
        try:
            await frame.delete_webhook(hook["id"])
        except frame.FrameAPIError as err:
            _die(f"Could not delete existing webhook: {err}")

    try:
        created = await frame.create_webhook(
            settings.frame_workspace_id,
            url=receiver_url,
            events=_WEBHOOK_EVENTS,
            name=_WEBHOOK_NAME,
        )
    except frame.FrameAPIError as err:
        _die(f"Webhook registration failed: {err}")

    secret = created.get("secret")
    if not secret:
        _die(
            "Webhook created but response did not include a `secret`. "
            "Inspect the response with /debug/frame/webhooks and use "
            "Frame's UI to retrieve it."
        )

    print()
    print("=" * 76)
    print("DONE — paste into .env (host machine):")
    print("=" * 76)
    print()
    print(f"FRAME_WEBHOOK_SECRET={secret}")
    print()
    print("Then on the host:")
    print("  docker compose up -d --force-recreate api")
    print()
    print(
        "Verify with: curl https://hub.<your-domain>/debug/frame/webhooks "
        "→ should list this webhook"
    )
    print()


if __name__ == "__main__":
    asyncio.run(main())
