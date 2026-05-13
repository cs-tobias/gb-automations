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
import base64
import hashlib
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import SyncCursor, User
from gb_automations.sync.sync_thread import sync_thread
from gb_automations.sync.watches import cursor_source_for

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


# ============================================================
# /webhooks/gmail — Gmail Pub/Sub push -> Notion sync (Stage 4c)
# ============================================================


def _verify_pubsub_jwt(authorization_header: str | None) -> dict[str, Any] | None:
    """Verify the OIDC JWT Pub/Sub sends with push deliveries.

    Returns the decoded claims on success, None on failure. Checks signature
    against Google's public keys, audience claim, and (if configured) issuer
    service account email.
    """
    if not authorization_header or not authorization_header.startswith("Bearer "):
        return None
    token = authorization_header.removeprefix("Bearer ").strip()
    try:
        claims = id_token.verify_oauth2_token(
            token, g_requests.Request(), audience=settings.pubsub_audience
        )
    except Exception:
        logger.exception("Pub/Sub JWT verification failed")
        return None

    if settings.pubsub_service_account_email:
        # Cross-check that the signer is the SA we expect (set during subscription creation)
        if claims.get("email") != settings.pubsub_service_account_email:
            logger.warning(
                "Pub/Sub JWT signer mismatch: expected=%s got=%s",
                settings.pubsub_service_account_email,
                claims.get("email"),
            )
            return None
    return claims


async def _load_history_cursor(email: str) -> str | None:
    async with SessionLocal() as session:
        cur = await session.get(SyncCursor, cursor_source_for(email))
        return cur.cursor_value if cur else None


async def _save_history_cursor(email: str, history_id: str) -> None:
    async with SessionLocal() as session:
        stmt = (
            pg_insert(SyncCursor)
            .values(source=cursor_source_for(email), cursor_value=str(history_id))
            .on_conflict_do_update(
                index_elements=["source"], set_={"cursor_value": str(history_id)}
            )
        )
        await session.execute(stmt)
        await session.commit()


def _collect_changed_thread_ids(history_response: dict[str, Any]) -> set[str]:
    """Extract every Gmail thread ID touched by the history response.

    We re-sync at the thread level (cheap, idempotent via dedup), so anything
    that mentions a thread — messageAdded, labelAdded — triggers a re-sync.
    """
    thread_ids: set[str] = set()
    for entry in history_response.get("history", []) or []:
        for change_type in ("messagesAdded", "labelsAdded"):
            for change in entry.get(change_type, []) or []:
                msg = change.get("message") or {}
                tid = msg.get("threadId")
                if tid:
                    thread_ids.add(tid)
    return thread_ids


@router.post("/gmail")
async def gmail_webhook(request: Request) -> Response:
    """Gmail Pub/Sub push receiver.

    Pub/Sub envelope shape:
      { "message": { "data": "<base64-json>", "messageId": "...", ... },
        "subscription": "projects/.../subscriptions/..." }
    The data, once base64-decoded, is:
      { "emailAddress": "...", "historyId": "..." }
    """
    claims = _verify_pubsub_jwt(request.headers.get("Authorization"))
    if claims is None:
        logger.warning("Pub/Sub push rejected: missing/invalid JWT")
        raise HTTPException(401, "Bad or missing Pub/Sub JWT")

    raw_body = await request.body()
    try:
        envelope = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON envelope") from None

    msg = envelope.get("message") or {}
    data_b64 = msg.get("data")
    if not data_b64:
        # Pub/Sub sometimes ping the endpoint with empty deliveries — ack quietly.
        return Response(content=json.dumps({"received": True}), media_type="application/json")

    try:
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as err:
        logger.exception("Failed to decode Pub/Sub data")
        raise HTTPException(400, f"Bad Pub/Sub data: {err}") from err

    email = (data.get("emailAddress") or "").lower()
    new_history_id = str(data.get("historyId") or "")
    if not email or not new_history_id:
        logger.warning("Pub/Sub message missing emailAddress or historyId: %s", data)
        return Response(content=json.dumps({"ignored": True}), media_type="application/json")

    logger.info("Gmail push: email=%s new_history_id=%s", email, new_history_id)

    # Look up the last historyId we processed for this user.
    last_history_id = await _load_history_cursor(email)
    if not last_history_id:
        # First push for this user — we don't know the starting point yet. Save the
        # incoming one as the baseline so subsequent pushes have something to diff against.
        # Skipping this single notification is fine; the watch() call returns the initial
        # historyId, which would normally be saved at watch-start time.
        await _save_history_cursor(email, new_history_id)
        return Response(
            content=json.dumps({"initialized": True, "history_id": new_history_id}),
            media_type="application/json",
        )

    # Fetch changes since last seen. history.list is sync — wrap in executor.
    loop = asyncio.get_running_loop()
    try:
        history = await loop.run_in_executor(
            _executor, partial(gmail_client.list_history, email, last_history_id)
        )
    except Exception:
        logger.exception("Gmail history.list failed for %s", email)
        raise HTTPException(502, "Gmail history fetch failed") from None

    thread_ids = _collect_changed_thread_ids(history)
    logger.info(
        "Gmail history for %s: from=%s to=%s threads=%d",
        email,
        last_history_id,
        new_history_id,
        len(thread_ids),
    )

    # Dispatch sync_thread for each affected thread. Each call is independent so
    # one bad thread doesn't kill the rest.
    synced = []
    errored = []
    for tid in thread_ids:
        try:
            result = await sync_thread(email, tid)
            synced.append({"thread_id": tid, "rows_created": result.rows_created})
        except Exception as err:
            logger.exception("sync_thread failed for %s / %s", email, tid)
            errored.append({"thread_id": tid, "error": str(err)})

    # Save the new cursor only after we attempted everything — that way we don't
    # advance past unprocessed history if Gmail returns a paginated response.
    await _save_history_cursor(email, new_history_id)

    return Response(
        content=json.dumps({"email": email, "synced": synced, "errored": errored}),
        media_type="application/json",
    )
