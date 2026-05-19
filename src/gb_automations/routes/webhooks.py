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
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.clients import notion_emails_db
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import ProjectLabel, SyncCursor, User
from gb_automations.obs import request_scope
from gb_automations.sync.sync_thread import sync_thread
from gb_automations.sync.watches import cursor_source_for
from gb_automations.utils.labels import project_label_path

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


_PROJECT_EVENT_TYPES = ("page.created", "page.properties_updated")


def _is_project_page(event: dict[str, Any]) -> bool:
    """True if this event refers to a page in the configured Projects DB.

    Accepts both `page.created` (new project → create label) and
    `page.properties_updated` (project renamed → rename label). The dispatcher
    branches on event type after this filter.

    When PROJECTS_DB_ID is set, only pages parented to that database qualify.
    Otherwise, accept any matching event regardless of parent.
    """
    if event.get("type") not in _PROJECT_EVENT_TYPES:
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


async def _create_label_for_all_users(
    notion_page_id: str, label_name: str
) -> dict[str, list[str]]:
    """Create the label in every active user's mailbox AND record the (project, user) → label_id
    mapping so a future rename can target each label by ID.
    """
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()

    succeeded: list[str] = []
    failed: list[str] = []
    label_ids_by_user: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    for user in users:
        try:
            label = await loop.run_in_executor(
                _executor, partial(gmail_client.create_label, user.email, label_name)
            )
            succeeded.append(user.email)
            label_ids_by_user[user.email] = label["id"]
        except Exception:
            logger.exception("Failed to create label %r for %s", label_name, user.email)
            failed.append(user.email)

    if label_ids_by_user:
        async with SessionLocal() as session:
            for email, label_id in label_ids_by_user.items():
                stmt = (
                    pg_insert(ProjectLabel)
                    .values(
                        notion_page_id=notion_page_id,
                        user_email=email,
                        gmail_label_id=label_id,
                        current_name=label_name,
                    )
                    .on_conflict_do_update(
                        index_elements=["notion_page_id", "user_email"],
                        set_={"gmail_label_id": label_id, "current_name": label_name},
                    )
                )
                await session.execute(stmt)
            await session.commit()

    return {"succeeded": succeeded, "failed": failed}


async def _rename_label_for_all_users(
    notion_page_id: str, new_name: str
) -> dict[str, Any]:
    """Rename the Gmail label across every user that has a stored mapping for this project.

    Returns a dict with keys: renamed (list[str]), failed (list[str]), and one of
    {unchanged: True, no_mapping: True} when the call was a no-op.
    """
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ProjectLabel).where(ProjectLabel.notion_page_id == notion_page_id)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        # Either the project predates this feature (run the backfill) or it's
        # genuinely a non-project event (page in another DB) we somehow accepted.
        return {"renamed": [], "failed": [], "no_mapping": True}

    if all(r.current_name == new_name for r in rows):
        return {"renamed": [], "failed": [], "unchanged": True}

    succeeded: list[str] = []
    failed: list[str] = []
    loop = asyncio.get_running_loop()
    for row in rows:
        if row.current_name == new_name:
            continue  # already up-to-date for this user
        try:
            await loop.run_in_executor(
                _executor,
                partial(
                    gmail_client.update_label_name, row.user_email, row.gmail_label_id, new_name
                ),
            )
            succeeded.append(row.user_email)
        except Exception:
            logger.exception(
                "Failed to rename label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)

    if succeeded:
        async with SessionLocal() as session:
            await session.execute(
                update(ProjectLabel)
                .where(ProjectLabel.notion_page_id == notion_page_id)
                .where(ProjectLabel.user_email.in_(succeeded))
                .values(current_name=new_name)
            )
            await session.commit()

    return {"renamed": succeeded, "failed": failed}


@router.post("/notion")
async def notion_webhook(request: Request) -> Response:
    """Notion webhook receiver.

    On first registration Notion sends a body containing `verification_token`.
    Echo it back (in the body AND in X-Notion-Verification-Token header) so Notion
    confirms the URL belongs to us. After that, every event is signed with HMAC-SHA256
    using that token as the key.
    """
    with request_scope("notion"):
        return await _notion_webhook_impl(request)


async def _notion_webhook_impl(request: Request) -> Response:
    logger.info("📓 Notion webhook received")

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
        "📓 Notion event: type=%s entity=%s parent_type=%s parent_id=%s",
        event_type,
        entity_id,
        parent.get("type"),
        parent.get("id"),
    )

    if not _is_project_page(payload):
        # Classify the ignore reason so the log is informative. The most common
        # source of "ignored" events is our own writes echoing back from Notion:
        # creating email rows and contacts fires page.created webhooks too.
        # With year-partitioned Emails DBs we check membership in the local
        # cache of known year DB IDs (populated by the year router as each
        # year's DB gets resolved/created).
        parent_id_clean = (parent.get("id") or "").replace("-", "").lower()
        known_emails_dbs = await notion_emails_db.all_known_db_ids()
        if parent_id_clean in known_emails_dbs:
            reason = "our own Emails-DB row write (feedback loop, fine)"
        elif parent_id_clean == settings.contacts_db_id.replace("-", "").lower():
            reason = "our own Contacts-DB row write (feedback loop, fine)"
        elif event_type not in _PROJECT_EVENT_TYPES:
            reason = f"event type {event_type!r} is not a project create/rename"
        else:
            reason = "parent is not the configured Projects DB"
        logger.info("↳ ignored: %s", reason)
        return Response(
            content=json.dumps({"ignored": True, "reason": reason}),
            media_type="application/json",
        )

    logger.info("↳ accepted; fetching page title from Notion…")

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
        logger.info("↳ page has no title yet, skipping label creation")
        return Response(
            content=json.dumps({"ignored": True, "reason": "page has no title"}),
            media_type="application/json",
        )

    # Year segment of the label path comes from Notion's immutable created_time,
    # so renames only change the leaf — the year stays pinned to the project's
    # creation year even if the title is edited across a year boundary.
    label_name = project_label_path(title, page.get("created_time"))

    if event_type == "page.created":
        logger.info("↳ creating Gmail label %r in all active mailboxes…", label_name)
        result = await _create_label_for_all_users(page_id, label_name)
        logger.info(
            "↳ done. label=%r succeeded=%d failed=%d",
            label_name,
            len(result["succeeded"]),
            len(result["failed"]),
        )
        return Response(
            content=json.dumps({"label": label_name, **result}),
            media_type="application/json",
        )

    # page.properties_updated — title may or may not have changed.
    logger.info("↳ properties updated; checking if label rename is needed…")
    rename_result = await _rename_label_for_all_users(page_id, label_name)
    if rename_result.get("unchanged"):
        logger.info("↳ no rename needed (label unchanged: %r)", label_name)
    elif rename_result.get("no_mapping"):
        logger.warning(
            "↳ no project_labels mapping for page %s — run backfill_project_labels script",
            page_id,
        )
    else:
        logger.info(
            "↳ done. new_label=%r renamed=%d failed=%d",
            label_name,
            len(rename_result["renamed"]),
            len(rename_result["failed"]),
        )
    return Response(
        content=json.dumps({"label": label_name, **rename_result}),
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
    that mentions a thread — messageAdded, labelAdded, labelRemoved — triggers
    a re-sync. labelRemoved is what catches "user fixed a mislabel": the
    add-side fires on the new project, the remove-side fires on the old one,
    and sync_thread's reconciliation re-points existing rows. Without
    labelsRemoved here, swap-only events (where the same user action toggles
    both labels but the add isn't routed to us) would silently leave rows
    pointing at the old project.
    """
    thread_ids: set[str] = set()
    for entry in history_response.get("history", []) or []:
        for change_type in ("messagesAdded", "labelsAdded", "labelsRemoved"):
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
    with request_scope("gmail"):
        return await _gmail_webhook_impl(request)


async def _gmail_webhook_impl(request: Request) -> Response:
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

    logger.info("📬 Gmail push received: email=%s new_historyId=%s", email, new_history_id)

    # Look up the last historyId we processed for this user.
    last_history_id = await _load_history_cursor(email)
    if not last_history_id:
        # First push for this user — we don't know the starting point yet. Save the
        # incoming one as the baseline so subsequent pushes have something to diff against.
        await _save_history_cursor(email, new_history_id)
        logger.info("↳ first push seen, saved as baseline (no sync yet)")
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
    if not thread_ids:
        logger.info("↳ no relevant changes since %s (nothing to sync)", last_history_id)
        await _save_history_cursor(email, new_history_id)
        return Response(
            content=json.dumps({"email": email, "synced": [], "errored": []}),
            media_type="application/json",
        )

    logger.info(
        "↳ found %d changed thread(s) between historyId %s → %s. Queuing background sync…",
        len(thread_ids),
        last_history_id,
        new_history_id,
    )

    # Hand off the actual sync work to a background task and ack Pub/Sub now.
    # Splitting a forwarded chain via the local LLM routinely takes 60–90s on
    # the M4 dev host; Pub/Sub's default ack deadline is 10s. Without this
    # fire-and-forget, Pub/Sub would redeliver while sync is still running,
    # causing duplicate work and stressing Ollama.
    asyncio.create_task(
        _run_thread_syncs_background(email, sorted(thread_ids), new_history_id)
    )

    return Response(
        content=json.dumps(
            {"email": email, "queued": len(thread_ids), "history_id": new_history_id}
        ),
        media_type="application/json",
    )


async def _run_thread_syncs_background(
    email: str, thread_ids: list[str], new_history_id: str
) -> None:
    """Background body of /webhooks/gmail — runs after we've ack'd Pub/Sub.

    Mirrors what the inline loop used to do: sync each thread, then save the
    cursor at the end so a partial run doesn't advance past unprocessed work.
    Errors are logged here rather than propagated — there's no HTTP request
    waiting for the result.
    """
    for tid in thread_ids:
        try:
            result = await sync_thread(email, tid)
            if result.project_page_id:
                logger.info(
                    "  ✓ thread %s [%s]: +%d rows, %d already present",
                    tid,
                    result.project_name,
                    result.rows_created,
                    result.rows_already_present,
                )
            else:
                logger.info(
                    "  ⊘ thread %s skipped: %s",
                    tid,
                    result.skipped_reason or "no project match",
                )
        except Exception:
            logger.exception("sync_thread failed for %s / %s", email, tid)

    try:
        await _save_history_cursor(email, new_history_id)
    except Exception:
        logger.exception("Failed to save history cursor for %s", email)
