"""Webhook receivers.

- /webhooks/echo  — logs and reflects whatever it gets; used to verify the
  Cloudflare Tunnel is wired up.
- /webhooks/notion — Notion → Gmail label flow. When a page is created in
  Notion (optionally scoped to a specific Projects database), create a Gmail
  label with the page's title in every active user's mailbox.

Stage 4c will add /webhooks/gmail (Pub/Sub push) for the inbound side.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])

# Gmail client is sync; offload to a thread pool to keep the event loop free.
_executor = ThreadPoolExecutor(max_workers=4)


# ============================================================
# /webhooks/echo — tunnel sanity check
# ============================================================


@router.post("/echo")
@router.get("/echo")
async def echo(request: Request) -> dict[str, Any]:
    """Logs and returns the request — used to verify Cloudflare Tunnel forwarding."""
    body_bytes = await request.body()
    try:
        body: Any = json.loads(body_bytes) if body_bytes else None
    except json.JSONDecodeError:
        body = body_bytes.decode("utf-8", errors="replace")

    payload = {
        "method": request.method,
        "url": str(request.url),
        "headers": dict(request.headers),
        "body": body,
    }
    logger.info("Echo webhook: %s", json.dumps(payload, default=str)[:2000])
    return {"received": True, **payload}


# ============================================================
# /webhooks/notion — Notion → Gmail label flow
# ============================================================


def _verify_notion_signature(body: bytes, signature_header: str | None) -> bool:
    """Constant-time HMAC-SHA256 check against NOTION_WEBHOOK_SECRET.

    Notion sends `X-Notion-Signature: sha256=<hex>` where the digest is HMAC-SHA256
    of the raw body using the verification token as key.
    """
    if not settings.notion_webhook_secret or not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    provided = signature_header.removeprefix("sha256=")
    expected = hmac.new(
        settings.notion_webhook_secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(provided, expected)


def _is_project_page(event: dict[str, Any]) -> bool:
    """True if this event refers to a page that should trigger label creation.

    When PROJECTS_DB_ID is set, only pages parented to that database qualify.
    Otherwise, accept any `page.created` event.
    """
    if event.get("type") != "page.created":
        return False
    if not settings.projects_db_id:
        return True

    # Webhook payload uses parent.type == "database" with the DB id in parent.id.
    # Page-parented or workspace-parented pages have different types ("page",
    # "workspace") and are not what we want.
    data = event.get("data") or {}
    parent = data.get("parent") or {}
    if parent.get("type") not in ("database", "database_id"):
        return False
    parent_id = (parent.get("id") or parent.get("database_id") or "").replace("-", "")
    target_id = settings.projects_db_id.replace("-", "")
    return parent_id == target_id


async def _create_label_for_all_users(label_name: str) -> dict[str, list[str]]:
    """Create the label in every active user's mailbox. Returns success/failure breakdown."""
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()

    succeeded: list[str] = []
    failed: list[str] = []
    loop = asyncio.get_running_loop()
    for user in users:
        try:
            await loop.run_in_executor(
                _executor, partial(gmail_client.create_label, user.email, label_name)
            )
            succeeded.append(user.email)
        except Exception:
            logger.exception("Failed to create label %r for %s", label_name, user.email)
            failed.append(user.email)
    return {"succeeded": succeeded, "failed": failed}


@router.post("/notion")
async def notion_webhook(request: Request) -> Response:
    """Notion webhook receiver.

    On first registration Notion sends a body containing `verification_token`.
    Echo it back (in the body AND in X-Notion-Verification-Token header) so Notion
    confirms the URL belongs to us. After that, every event is signed with HMAC-SHA256
    using that token as the key.
    """
    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body") from None

    # Phase 1: verification handshake (URL ownership check). Notion sends a token,
    # we echo it back. The token also becomes NOTION_WEBHOOK_SECRET going forward.
    verification_token = payload.get("verification_token")
    if verification_token:
        logger.warning(
            "Notion webhook verification received. Paste this into NOTION_WEBHOOK_SECRET in .env "
            "(then docker compose up -d --force-recreate api): %s",
            verification_token,
        )
        return Response(
            content=json.dumps({"verification_token": verification_token}),
            media_type="application/json",
            headers={"X-Notion-Verification-Token": verification_token},
        )

    # Phase 2: signed event. Verify signature before doing anything.
    signature = request.headers.get("X-Notion-Signature") or request.headers.get(
        "x-notion-signature"
    )
    if not _verify_notion_signature(raw_body, signature):
        logger.warning(
            "Notion webhook signature mismatch (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        raise HTTPException(401, "Bad or missing signature")

    event_type = payload.get("type", "unknown")
    entity_id = (payload.get("entity") or {}).get("id")
    parent = (payload.get("data") or {}).get("parent") or {}
    logger.info(
        "Notion event: type=%s entity=%s parent_type=%s parent_id=%s",
        event_type,
        entity_id,
        parent.get("type"),
        parent.get("id"),
    )

    if not _is_project_page(payload):
        return Response(
            content=json.dumps({"ignored": True, "reason": f"not a project page ({event_type})"}),
            media_type="application/json",
        )

    # Pull the page ID from the event and fetch the page to read its current title.
    # The webhook payload often omits the full properties, so fetching is safer.
    entity = payload.get("entity") or {}
    page_id = entity.get("id") or (payload.get("data") or {}).get("id")
    if not page_id:
        raise HTTPException(400, "No page id in event payload")

    try:
        page = await notion_client.get_page(page_id)
    except Exception as err:
        logger.exception("Failed to fetch Notion page %s", page_id)
        raise HTTPException(502, f"Notion fetch failed: {err}") from err

    title = notion_client.extract_page_title(page)
    if not title:
        return Response(
            content=json.dumps({"ignored": True, "reason": "page has no title"}),
            media_type="application/json",
        )

    result = await _create_label_for_all_users(title)
    logger.info(
        "Created Gmail label %r for users: succeeded=%s failed=%s",
        title,
        result["succeeded"],
        result["failed"],
    )
    return Response(
        content=json.dumps({"label": title, **result}),
        media_type="application/json",
    )
