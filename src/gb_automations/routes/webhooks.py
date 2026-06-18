"""Webhook receivers.

- /webhooks/echo  — logs and reflects whatever it gets; used to verify the
  Cloudflare Tunnel is wired up.
- /webhooks/notion — Notion → Gmail label flow. Triggered by a "Sync to Gmail"
  Notion button on the Projects DB: POST with bearer auth and a JSON body
  {"page_id": "<id>"}. ENQUEUES a `label_sync` task and returns — the durable
  queue worker creates/reconciles the Gmail label across mailboxes (+ the NAS
  folder). Does NOT do that work inline: it used to, which intermittently
  exceeded Notion's button-webhook timeout. The label engine lives in
  sync/sync_labels.py.

- /webhooks/gmail — inbound side (Gmail Pub/Sub push). Does NOT sync inline: it
  ENQUEUES a durable `sync_tasks` row and advances the history cursor in one
  transaction, then acks. A background worker (jobs/queue_worker.py, started in
  main.py's lifespan) drains the queue and runs sync_thread with retry/backoff.
- /webhooks/notion/resync-thread — per-email "Re-sync" button on the Emails DB:
  enqueues a REBUILD of that thread (archive its rows + recreate under current
  code). Same queue, same worker.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
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
from gb_automations.config import (
    DISCIPLINE_KEYS,
    EMAILS_PROPS,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.jobs import queue_worker
from gb_automations.models import EmailRow, SyncCursor, User
from gb_automations.obs import request_scope
from gb_automations.sync import queue_mirror
from gb_automations.sync import resync_project as resync_project_mod
from gb_automations.sync.queue import (
    enqueue_send_faktura,
    enqueue_frame_comment_sync,
    enqueue_frame_file_status_sync,
    enqueue_frame_leveranse_sync,
    enqueue_frame_project_sync,
    enqueue_frame_version_sync,
    enqueue_label_sync,
    enqueue_nas_folder_sync,
    enqueue_oppgave_done_sync,
    enqueue_project_status_dispatch,
    enqueue_task_folder_sync,
    enqueue_threads,
    enqueue_toggl_project_sync,
)
from gb_automations.sync.watches import cursor_source_for, fetch_project_label_ids_for_user

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


def _verify_bearer(auth_header: str | None) -> bool:
    """Constant-time check of the Authorization header against NOTION_WEBHOOK_SECRET.

    Notion buttons let you set a custom Authorization header; we use that as the
    sole auth for this endpoint (button webhooks are NOT HMAC-signed the way
    database-event subscriptions are).

    Accept either `Bearer <secret>` or the bare `<secret>` — Notion's UI doesn't
    enforce a scheme, so operators may paste the raw secret in the Value field.
    Compare both candidates in constant time to avoid leaking which form matched.
    """
    if not settings.notion_webhook_secret or not auth_header:
        return False
    secret = settings.notion_webhook_secret
    match_bearer = hmac.compare_digest(auth_header, f"Bearer {secret}")
    match_bare = hmac.compare_digest(auth_header, secret)
    return match_bearer or match_bare


def _page_parented_to_projects_db(page: dict[str, Any]) -> bool:
    """Parent check: page is a row in the configured Projects database.

    When PROJECTS_DB_ID is unset (some local/dev workspaces), skip the check —
    the button can only be placed on the Projects DB anyway.

    The REST API returns `parent.type == "database_id"` with the id in
    `parent.database_id`. Notion's button-webhook envelope uses
    `parent.type == "data_source_id"` but still includes `database_id` as a
    sibling field. We just look for `database_id` regardless of `type`.
    """
    if not settings.projects_db_id:
        return True
    parent = page.get("parent") or {}
    parent_db = (parent.get("database_id") or "").replace("-", "").lower()
    target = settings.projects_db_id.replace("-", "").lower()
    return parent_db == target


def _page_parented_to_oppgaver_db(page: dict[str, Any]) -> bool:
    """Parent check: page is a row in the configured Oppgaver database.

    Unlike the Projects check, an unset OPPGAVER_DB_ID means "Oppgaver-row
    webhooks disabled" rather than "skip the check" — we never want to
    misroute a project-button click as an Oppgaver-folder sync. Returns
    False if unset.
    """
    if not settings.oppgaver_db_id:
        return False
    parent = page.get("parent") or {}
    parent_db = (parent.get("database_id") or "").replace("-", "").lower()
    target = settings.oppgaver_db_id.replace("-", "").lower()
    return parent_db == target


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(
        content=json.dumps(payload), media_type="application/json", status_code=status
    )


# In-memory last-seen tracker for the three Notion-automation receivers
# (project-status, oppgave-done, oppgave-status). Exposed via
# GET /debug/notion-automation-health — lets the operator confirm Notion
# is still firing each automation without having to flip a Status by hand
# and check logs. Resets on every process restart (a 5d uptime container
# can show 5d of history; a fresh container reads as "never seen yet").
#
# Each entry is {"last_seen_utc": iso8601, "last_action": str, "count": int}.
# `count` is the per-process call counter so a sudden jump from 47 → 47 over
# a 24h window screams "Notion paused this one." `last_action` mirrors what
# the receiver returned (queued / already_queued / auth_failed / etc.) so a
# spike of `auth_failed` shows up at a glance.
_NOTION_AUTOMATION_LAST_SEEN: dict[str, dict[str, Any]] = {}


def _record_notion_automation_hit(name: str, action: str) -> None:
    """Best-effort record of a Notion-automation receiver call. Called from
    every code path in the three receivers (success, skip, auth fail, bad
    JSON) so the debug endpoint sees ALL the activity, not just the happy
    path. Cheap — a dict update on the hot path, no I/O."""
    from datetime import UTC, datetime

    entry = _NOTION_AUTOMATION_LAST_SEEN.setdefault(
        name, {"last_seen_utc": None, "last_action": None, "count": 0}
    )
    entry["last_seen_utc"] = datetime.now(UTC).isoformat()
    entry["last_action"] = action
    entry["count"] += 1


@router.post("/notion")
async def notion_webhook(request: Request) -> Response:
    """Notion "Sync to Gmail" button receiver.

    The Notion Projects DB has a Button property whose automation sends a POST
    here with `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` and JSON body
    `{"page_id": "<id>"}` (Notion substitutes `{{page.id}}` per click).

    Idempotent: creates the label first time, renames if the title changed,
    reconciles any per-user rows that drifted (e.g. a workspace user was added
    after the project was first synced).
    """
    with request_scope("notion"):
        return await _notion_webhook_impl(request)


async def _owning_email_for_thread(thread_id: str) -> str | None:
    """Which mailbox should re-sync this thread? Prefer the one that first saw
    it (EmailRow.seen_by_email); else any active user (the thread is shared, and
    sync_thread fetches it via DWD impersonation, so any active mailbox works).
    """
    async with SessionLocal() as session:
        seen = (
            await session.execute(
                select(EmailRow.seen_by_email)
                .where(EmailRow.gmail_thread_id == thread_id, EmailRow.seen_by_email.isnot(None))
                .limit(1)
            )
        ).scalar_one_or_none()
        if seen:
            return seen
        return (
            await session.execute(select(User.email).where(User.active.is_(True)).limit(1))
        ).scalar_one_or_none()


async def _resync_thread_impl(request: Request) -> Response:
    if not _verify_bearer(request.headers.get("Authorization")):
        raise HTTPException(401, "Bad or missing bearer token")

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body") from None

    page_id = ((payload.get("data") or {}).get("id") or "").strip()
    if not page_id:
        raise HTTPException(400, "Missing data.id in Notion button payload")

    # Re-fetch the Emails row to read its Thread ID (the button envelope already
    # carries properties, but re-fetching avoids depending on the envelope shape).
    try:
        page = await notion_client.get_page(page_id)
    except Exception as err:
        logger.exception("Failed to fetch Notion page %s", page_id)
        raise HTTPException(502, f"Notion fetch failed: {err}") from err

    thread_id = notion_client.read_rich_text_prop(page, EMAILS_PROPS["thread_id"])
    if not thread_id:
        logger.warning("↳ page %s has no %s — can't re-sync", page_id, EMAILS_PROPS["thread_id"])
        return _json(
            {"page_id": page_id, "action": "skipped", "reason": "no Thread ID on this row"}
        )

    owner = await _owning_email_for_thread(thread_id)
    if not owner:
        return _json(
            {"thread_id": thread_id, "action": "skipped", "reason": "no active mailbox to sync"}
        )

    # Just a queue add — but flagged as a REBUILD so the worker archives the
    # thread's existing rows and recreates them fresh under current code (body/
    # tags/splitting all regenerate), rather than a plain repair-in-place. The
    # rebuild rides the same queue: retries, dot, visibility, all for free.
    # Contacts/Companies are never deleted (only re-matched by the sync).
    inserted = await enqueue_threads(owner, [thread_id], rebuild=True)
    queue_worker.wake()
    await queue_mirror.mark_queued(thread_id, subject=thread_id)
    logger.info(
        "🔁 rebuild requested for thread %s (owner %s) — %s",
        thread_id,
        owner,
        "enqueued" if inserted else "already queued",
    )
    return _json(
        {
            "thread_id": thread_id,
            "owner": owner,
            "action": "rebuilding" if inserted else "already_queued",
        }
    )


@router.post("/notion/resync-thread")
async def notion_resync_thread(request: Request) -> Response:
    """Per-email "Re-sync" button receiver (on the Emails DB).

    A Button property on each Emails row POSTs here with the bearer token and
    the row's page id. We read the row's `Thread ID` and enqueue a fresh sync
    for that thread — the worker re-runs sync_thread, which repairs existing
    rows in place (idempotent, Notion-backed dedup) and now also self-heals any
    stale cached ids. Use it to redo a thread that synced but looks wrong, or to
    re-run it under new code. Idempotent: a thread already queued is a no-op.
    """
    with request_scope("resync"):
        return await _resync_thread_impl(request)


async def _resync_project_impl(request: Request) -> Response:
    if not _verify_bearer(request.headers.get("Authorization")):
        raise HTTPException(401, "Bad or missing bearer token")

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body") from None

    page_id = ((payload.get("data") or {}).get("id") or "").strip()
    if not page_id:
        raise HTTPException(400, "Missing data.id in Notion button payload")

    # Re-fetch the page to guard that the click came from the Projects DB (not a
    # stray button elsewhere), same as the "Sync to Gmail" button does.
    try:
        page = await notion_client.get_page(page_id)
    except Exception as err:
        logger.exception("Failed to fetch Notion page %s", page_id)
        raise HTTPException(502, f"Notion fetch failed: {err}") from err

    if not _page_parented_to_projects_db(page):
        logger.warning("↳ page %s is not a Projects DB row — ignoring", page_id)
        return _json(
            {"page_id": page_id, "action": "skipped", "reason": "not a Projects DB row"}
        )

    # Fan the per-email "Re-sync" pattern out over every thread in the project:
    # resolve the project's threads and drop them on the durable queue with
    # rebuild=True. The worker archives + recreates each thread under current
    # code, exactly like a normal sync — same logging, retries and status dot.
    summary = await resync_project_mod.enqueue_project(page_id, rebuild=True)
    queue_worker.wake()
    logger.info(
        "🔁 resync project requested for %s — enqueued %d of %d thread(s)",
        page_id,
        summary["enqueued"],
        summary["threads"],
    )
    return _json({"page_id": page_id, "action": "resyncing", **summary})


@router.post("/notion/resync-project")
async def notion_resync_project(request: Request) -> Response:
    """Per-project "Resync Project" button receiver (on the Projects DB).

    The project equivalent of the per-email "Re-sync" button: a Button property
    on each Projects row POSTs here with the bearer token and the row's page id.
    We enumerate every Gmail thread under the project's label(s) and enqueue them
    all for a rebuild — the queue worker drains them like any other sync. No
    inline work, so the webhook returns immediately. Idempotent: threads already
    queued are skipped.
    """
    with request_scope("resync-project"):
        return await _resync_project_impl(request)


async def _resolve_project_button_click(request: Request) -> tuple[str, str]:
    """Shared front-half of the per-system Projects-DB buttons.

    Verifies the bearer token, extracts the page id from the Notion envelope,
    fetches the page, checks it's a Projects-DB row, and reads its title.
    Returns (page_id, title); raises HTTPException on any validation failure
    (the FastAPI handler then surfaces the right status code automatically).

    Factored out because the three per-system endpoints
    (/webhooks/notion/sync-gmail | sync-nas | sync-frame) all share this
    pre-flight — the only thing that varies is which enqueue_* they call.
    """
    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion button auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        raise HTTPException(401, "Bad or missing bearer token")

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body") from None

    page_id = ((payload.get("data") or {}).get("id") or "").strip()
    if not page_id:
        raise HTTPException(400, "Missing data.id in Notion button payload")

    try:
        page = await notion_client.get_page(page_id)
    except Exception as err:
        logger.exception("Failed to fetch Notion page %s", page_id)
        raise HTTPException(502, f"Notion fetch failed: {err}") from err

    if not _page_parented_to_projects_db(page):
        logger.warning("↳ page %s is not in the Projects DB; rejecting", page_id)
        raise HTTPException(
            400, "page is not in the configured Projects DB"
        )

    title = notion_client.extract_page_title(page)
    if not title:
        raise HTTPException(400, "page has no title yet")
    return page_id, title


async def _sync_gmail_impl(request: Request) -> Response:
    page_id, title = await _resolve_project_button_click(request)
    inserted = await enqueue_label_sync(page_id)
    queue_worker.wake()
    logger.info(
        "🏷  gmail-only sync requested for %r (page %s) — %s",
        title,
        page_id,
        "enqueued" if inserted else "already queued",
    )
    return _json(
        {
            "page_id": page_id,
            "kind": "label_sync",
            "action": "queued" if inserted else "already_queued",
        }
    )


async def _sync_nas_impl(request: Request) -> Response:
    page_id, title = await _resolve_project_button_click(request)
    if not (settings.sync_nas_folders and settings.nas_projects_root):
        logger.info(
            "↳ NAS sync click for %r ignored — sync_nas_folders=%s nas_projects_root=%r",
            title,
            settings.sync_nas_folders,
            settings.nas_projects_root,
        )
        return _json(
            {
                "page_id": page_id,
                "kind": "nas_folder_sync",
                "action": "skipped",
                "reason": "NAS sync is disabled or unconfigured",
            }
        )
    inserted = await enqueue_nas_folder_sync(page_id)
    queue_worker.wake()
    logger.info(
        "📁 nas-only sync requested for %r (page %s) — %s",
        title,
        page_id,
        "enqueued" if inserted else "already queued",
    )
    return _json(
        {
            "page_id": page_id,
            "kind": "nas_folder_sync",
            "action": "queued" if inserted else "already_queued",
        }
    )


async def _enqueue_frame_deliverables_for_project(page_id: str) -> tuple[int, int]:
    """Fan out `frame_leveranse_sync` over every recognized-discipline
    deliverable under the given project.

    Returns `(leveranse_queued, deliverables_total)` — the first is how many
    rows produced a newly-inserted queue task, the second is how many Oppgaver
    rows we walked (incl. internal tasks we skipped). The discipline gate
    (`Type` ∈ DISCIPLINE_KEYS) mirrors the per-row Sync button's filter so
    internal tasks (Klargjøre modell / blank Type / Korreksjonsrunde sub-rows)
    are not enqueued — sync_frame_leveranse would no-op them anyway.

    Shared between the manual "Sync Frame" button (`_sync_frame_impl`) and the
    status-driven `/notion/project-status` automation — keeps the gate logic
    in one place.
    """
    leveranser = await notion_client.oppgaver_for_project(page_id)
    leveranse_queued = 0
    for row in leveranser:
        discipline = notion_client.task_discipline(row)
        if not DISCIPLINE_KEYS.get((discipline or "").strip().lower()):
            continue
        leveranse_queued += await enqueue_frame_leveranse_sync(row["id"])
    return leveranse_queued, len(leveranser)


async def _sync_frame_impl(request: Request) -> Response:
    page_id, title = await _resolve_project_button_click(request)
    if not settings.sync_frame:
        logger.info("↳ Frame sync click for %r ignored — SYNC_FRAME=false", title)
        return _json(
            {
                "page_id": page_id,
                "kind": "frame_project_sync",
                "action": "skipped",
                "reason": "SYNC_FRAME=false",
            }
        )
    inserted = await enqueue_frame_project_sync(page_id)
    # Fan out: sync every deliverable in this project too, so one button on the
    # Projects DB provisions the project + ALL its Frame placeholders. The
    # per-deliverable button still works for re-syncing a single row; both share
    # the same idempotent enqueue, so re-clicking either is a no-op.
    leveranse_queued, leveranser_total = await _enqueue_frame_deliverables_for_project(
        page_id
    )
    queue_worker.wake()
    logger.info(
        "🎬 frame-only sync requested for %r (page %s) — project=%s leveranser_queued=%d (of %d deliverables)",
        title,
        page_id,
        "enqueued" if inserted else "already queued",
        leveranse_queued,
        leveranser_total,
    )
    return _json(
        {
            "page_id": page_id,
            "kind": "frame_project_sync",
            "action": "queued" if inserted else "already_queued",
            "leveranser_queued": leveranse_queued,
        }
    )


async def _sync_toggl_impl(request: Request) -> Response:
    page_id, title = await _resolve_project_button_click(request)
    if not settings.sync_toggl:
        logger.info("↳ Toggl sync click for %r ignored — SYNC_TOGGL=false", title)
        return _json(
            {
                "page_id": page_id,
                "kind": "toggl_project_sync",
                "action": "skipped",
                "reason": "SYNC_TOGGL=false",
            }
        )
    inserted = await enqueue_toggl_project_sync(page_id)
    queue_worker.wake()
    logger.info(
        "⏱  toggl-only sync requested for %r (page %s) — %s",
        title,
        page_id,
        "enqueued" if inserted else "already queued",
    )
    return _json(
        {
            "page_id": page_id,
            "kind": "toggl_project_sync",
            "action": "queued" if inserted else "already_queued",
        }
    )


@router.post("/notion/sync-gmail")
async def notion_sync_gmail(request: Request) -> Response:
    """Per-system "Sync Gmail labels" button receiver (on the Projects DB).

    Enqueues a single `label_sync` task for the clicked project — no NAS, no
    Frame.io fan-out. Idempotent: a project already being label-synced is a
    no-op (dedup'd via uq_sync_tasks_active_label). Pair with the global
    Initialize button when the team wants to (re)provision just one system.
    """
    with request_scope("sync-gmail"):
        return await _sync_gmail_impl(request)


@router.post("/notion/sync-nas")
async def notion_sync_nas(request: Request) -> Response:
    """Per-system "Sync NAS" button receiver (on the Projects DB).

    Enqueues a single `nas_folder_sync` task for the clicked project — no
    Gmail, no Frame.io fan-out. Idempotent. Returns `skipped` when NAS is
    disabled (sync_nas_folders=false) or unconfigured (no NAS_PROJECTS_ROOT)
    so the Notion-side automation surface is obvious without the click
    silently doing nothing.
    """
    with request_scope("sync-nas"):
        return await _sync_nas_impl(request)


@router.post("/notion/sync-frame")
async def notion_sync_frame(request: Request) -> Response:
    """Per-system "Sync Frame.io" button receiver (on the Projects DB).

    Enqueues a single `frame_project_sync` task for the clicked project — no
    Gmail, no NAS fan-out. Idempotent. Returns `skipped` when Frame
    integration is off (SYNC_FRAME=false).
    """
    with request_scope("sync-frame"):
        return await _sync_frame_impl(request)


@router.post("/notion/sync-toggl")
async def notion_sync_toggl(request: Request) -> Response:
    """Per-system "Sync Toggl" button receiver (on the Projects DB).

    Enqueues a single `toggl_project_sync` task for the clicked project — no
    Gmail, no NAS, no Frame fan-out. Idempotent. Returns `skipped` when the
    Toggl integration is off (SYNC_TOGGL=false), so a click on a misconfigured
    deploy fails visibly instead of silently doing nothing.
    """
    with request_scope("sync-toggl"):
        return await _sync_toggl_impl(request)


@router.post("/notion/oppgave-done")
async def notion_oppgave_done(request: Request) -> Response:
    """Notion automation receiver: the team toggled `Ferdig` on an Oppgaver row.

    Auth: `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` — same secret the
    other Notion buttons use. Notion's automation UI supports custom request
    headers, so we reuse the bearer pattern rather than an HMAC scheme.

    Payload shape (verified probe 2026-05-27):
        {"source": {...}, "data": {"object": "page", "id": "<uuid>",
            "parent": {"database_id": "<oppgaver db id>"}, ...,
            "properties": {}}}  ← properties is EMPTY; the engine GETs the
                                  row at process time to read the checkbox.

    Two Notion automations should be set up — one for "Ferdig is checked",
    one for "Ferdig is unchecked" — both POSTing here with the same secret.
    Direction-agnostic: the engine reads the row's current state regardless
    of which automation fired.
    """
    with request_scope("oppgave-done"):
        return await _notion_oppgave_done_impl(request)


async def _notion_oppgave_done_impl(request: Request) -> Response:
    # Always 200 — same rationale as _notion_project_status_impl (Notion
    # auto-pauses webhook automations on any non-2xx, single failure can
    # trip it, no retry layer). Auth + JSON failures are logged loud here
    # but never surface to Notion as errors.
    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion oppgave-done auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        _record_notion_automation_hit("oppgave-done", "auth_failed")
        return _json({"action": "skipped", "reason": "auth failed"})

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning("oppgave-done: invalid JSON body")
        _record_notion_automation_hit("oppgave-done", "invalid_json")
        return _json({"action": "skipped", "reason": "invalid JSON"})

    # Notion automation payload puts the page object under `data` (not
    # `data.id` like buttons — same outer shape, different content).
    data = payload.get("data") or {}
    page_id = data.get("id")
    parent = (data.get("parent") or {}).get("database_id") or ""
    target_db = (settings.korreksjoner_db_id or "").replace("-", "").lower()
    payload_db = parent.replace("-", "").lower()

    if not page_id:
        logger.warning("oppgave-done: no page id in payload")
        _record_notion_automation_hit("oppgave-done", "no_page_id")
        return _json({"action": "skipped", "reason": "no page id"})

    # Defense: confirm the row really is in the Korreksjoner DB (the
    # automation fires on a Korreksjon row's Ferdig toggle, so it should
    # only target that DB; a misconfigured automation pointing at another
    # DB would silently send junk events here otherwise).
    if target_db and payload_db != target_db:
        logger.info(
            "oppgave-done: page %s parent DB %s does not match KORREKSJONER_DB_ID — skipping",
            page_id,
            parent,
        )
        _record_notion_automation_hit("oppgave-done", "wrong_parent_db")
        return _json({"action": "skipped", "reason": "wrong parent DB"})

    inserted = await enqueue_oppgave_done_sync(page_id)
    queue_worker.wake()
    logger.info(
        "📥 enqueued oppgave_done_sync for %s (inserted=%d)",
        page_id,
        inserted,
    )
    _record_notion_automation_hit(
        "oppgave-done", "enqueued" if inserted else "already_queued"
    )
    return _json(
        {
            "action": "enqueued",
            "oppgave_page_id": page_id,
            "enqueued": inserted,
        }
    )


@router.post("/notion/oppgave-status")
async def notion_oppgave_status(request: Request) -> Response:
    """Notion automation receiver: a deliverable's Status select changed.

    Auth: `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` (same secret
    as the Ferdig automation). The automation should be set up on the
    Oppgaver DB with trigger "Status changes" — the engine reads the
    row's current Status at process time, so a single automation
    covers entering AND leaving Utgår.

    Payload shape mirrors /webhooks/notion/oppgave-done:
        {"source": {...}, "data": {"object": "page", "id": "<uuid>",
            "parent": {"database_id": "<oppgaver db id>"}, ...}}
    """
    with request_scope("oppgave-status"):
        return await _notion_oppgave_status_impl(request)


async def _notion_oppgave_status_impl(request: Request) -> Response:
    # Always 200 — see _notion_project_status_impl rationale.
    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion oppgave-status auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        _record_notion_automation_hit("oppgave-status", "auth_failed")
        return _json({"action": "skipped", "reason": "auth failed"})

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning("oppgave-status: invalid JSON body")
        _record_notion_automation_hit("oppgave-status", "invalid_json")
        return _json({"action": "skipped", "reason": "invalid JSON"})

    data = payload.get("data") or {}
    page_id = data.get("id")
    parent = (data.get("parent") or {}).get("database_id") or ""
    target_db = (settings.oppgaver_db_id or "").replace("-", "").lower()
    payload_db = parent.replace("-", "").lower()

    if not page_id:
        logger.warning("oppgave-status: no page id in payload")
        _record_notion_automation_hit("oppgave-status", "no_page_id")
        return _json({"action": "skipped", "reason": "no page id"})

    # Defense: confirm the row really is in the Oppgaver DB (the
    # automation should fire on the Oppgaver DB; a misconfigured
    # automation pointing at another DB would silently send junk events
    # here otherwise).
    if target_db and payload_db != target_db:
        logger.info(
            "oppgave-status: page %s parent DB %s does not match OPPGAVER_DB_ID — skipping",
            page_id,
            parent,
        )
        _record_notion_automation_hit("oppgave-status", "wrong_parent_db")
        return _json({"action": "skipped", "reason": "wrong parent DB"})

    inserted = await enqueue_frame_file_status_sync(page_id, source="notion")
    queue_worker.wake()
    logger.info(
        "📥 enqueued frame_file_status_sync for %s (source=notion, inserted=%d)",
        page_id,
        inserted,
    )
    _record_notion_automation_hit(
        "oppgave-status", "enqueued" if inserted else "already_queued"
    )
    return _json(
        {
            "action": "enqueued",
            "leveranse_page_id": page_id,
            "source": "notion",
            "enqueued": inserted,
        }
    )


@router.post("/notion/project-status")
async def notion_project_status(request: Request) -> Response:
    """Notion automation receiver: a Project's `Status` select changed.

    THIS ENDPOINT IS DELIBERATELY MINIMAL — bearer check, parent-DB sanity
    check, enqueue ONE `project_status_dispatch` task, return 200. The actual
    work (Notion get_page, placeholder-title gate, status read, fan-out to
    gmail/nas/toggl/frame + per-leveranse Frame + the active/inactive lane)
    runs on the queue worker via `sync/dispatch_project_status.py`.

    Why this shape: Notion auto-pauses webhook automations whose receiver
    takes too long to respond (the timeout isn't published; community
    reports + n8n issue #12257 indicate Notion's pause heuristic is
    over-eager and fires on slow 200s, not just 5xx). The earlier inline
    shape did 2 Notion API calls + 5 Postgres inserts before responding,
    which on Goldbox's prod workspace was tripping the pause. Sub-50ms ack
    keeps us safely under whatever the threshold is.

    Auth: `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` (same secret every
    other Notion automation/button uses). The companion Notion automation
    must be configured on the Projects DB with trigger "Property edited"
    scoped to the `Status` property — that scoping is what stops every
    other edit on a project row (Frame/NAS/Toggl URL writebacks, the Sync
    icon, Sync progress, etc.) from firing this endpoint.

    Per-engine semantics, placeholder-title gate, "no Name-edited automation"
    rationale, etc. — all moved to the dispatcher's docstring. See
    `sync/dispatch_project_status.py`.
    """
    with request_scope("project-status"):
        return await _notion_project_status_impl(request)


async def _notion_project_status_impl(request: Request) -> Response:
    # IMPORTANT: every code path below MUST return 200, regardless of whether
    # we did real work, skipped, or rejected the call. Notion's "Send webhook"
    # action auto-pauses automations whose receiver responds with any non-2xx
    # — the threshold is undocumented (see research notes in plan file
    # `stabilise-notion-status-change-automation-against-auto-pause`), there
    # is no retry layer, and a *single* failure can flip the pause indicator.
    # Auth failure and bad JSON stay loud in OUR logs so a real misconfig is
    # debuggable; Notion just sees a 200 with a reason in the body and keeps
    # firing the automation. Visibility for the operator: GET
    # /debug/notion-automation-health shows last_seen + last_action so a
    # sustained auth_failed spike is one curl away.
    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion project-status auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        _record_notion_automation_hit("project-status", "auth_failed")
        return _json({"action": "skipped", "reason": "auth failed"})

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning("project-status: invalid JSON body")
        _record_notion_automation_hit("project-status", "invalid_json")
        return _json({"action": "skipped", "reason": "invalid JSON"})

    data = payload.get("data") or {}
    page_id = (data.get("id") or "").strip()
    if not page_id:
        logger.warning("project-status: no page id in payload")
        _record_notion_automation_hit("project-status", "no_page_id")
        return _json({"action": "skipped", "reason": "no page id"})

    # Defense: confirm the row really is in the Projects DB. The automation
    # is supposed to live on that DB, but a misconfigured automation pointing
    # at another DB would silently send junk events here otherwise — same
    # guard pattern as /notion/oppgave-status. Cheap (string compare on the
    # automation payload); does NOT require a Notion API call.
    parent = (data.get("parent") or {}).get("database_id") or ""
    target_db = (settings.projects_db_id or "").replace("-", "").lower()
    payload_db = parent.replace("-", "").lower()
    if target_db and payload_db != target_db:
        logger.info(
            "project-status: page %s parent DB %s does not match PROJECTS_DB_ID — skipping",
            page_id,
            parent,
        )
        _record_notion_automation_hit("project-status", "wrong_parent_db")
        return _json(
            {
                "page_id": page_id,
                "action": "skipped",
                "reason": "wrong parent DB",
            }
        )

    inserted = await enqueue_project_status_dispatch(page_id)
    queue_worker.wake()
    action = "queued" if inserted else "already_queued"
    logger.info(
        "📥 project-status dispatch %s for %s",
        "enqueued" if inserted else "already_queued",
        page_id,
    )
    _record_notion_automation_hit("project-status", action)
    return _json(
        {
            "page_id": page_id,
            "kind": "project_status_dispatch",
            "action": action,
        }
    )


# ============================================================
# /webhooks/notion/send-faktura — Notion button receiver
# ============================================================


@router.post("/notion/send-faktura")
async def notion_send_faktura(request: Request) -> Response:
    """Notion button receiver: "Send faktura" on a Project row.

    One button per project — no oppstart/slutt argument. The worker
    reads the project's `Faktura status` to decide whether this click
    bills 50% (status = "Til oppstartsfaktura") or the remainder
    (status = "Til avslutningsfaktura"). The "what to bill" picker is
    on the Oppgaver themselves (each row's `Fakturert status` select).

    Same minimal shape as /webhooks/notion/project-status: bearer check,
    body parse, ENQUEUE a `send_faktura` task, return 200. All real work
    (Notion reads, Fiken POSTs, Notion stampbacks) runs on the queue
    worker — keeps us safely under Notion's auto-pause threshold for
    slow receivers.

    Auth: same `NOTION_WEBHOOK_SECRET` every other Notion automation
    uses. Like the other Notion-automation receivers, this returns
    HTTP 200 for EVERY code path including auth/JSON failure — Notion
    auto-pauses on any non-2xx, and the threshold is undocumented.
    """
    with request_scope("send-faktura"):
        return await _notion_send_faktura_impl(request)


async def _notion_send_faktura_impl(request: Request) -> Response:
    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion send-faktura auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        _record_notion_automation_hit("send-faktura", "auth_failed")
        return _json({"action": "skipped", "reason": "auth failed"})

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        logger.warning("send-faktura: invalid JSON body")
        _record_notion_automation_hit("send-faktura", "invalid_json")
        return _json({"action": "skipped", "reason": "invalid JSON"})

    data = payload.get("data") or {}
    page_id = (data.get("id") or "").strip()
    if not page_id:
        logger.warning("send-faktura: no page id in payload")
        _record_notion_automation_hit("send-faktura", "no_page_id")
        return _json({"action": "skipped", "reason": "no page id"})

    # Parent-DB sanity check — same as project-status. Cheap string
    # compare; doesn't hit Notion. Skipped when PROJECTS_DB_ID is unset
    # (local/dev workspaces).
    parent = (data.get("parent") or {}).get("database_id") or ""
    target_db = (settings.projects_db_id or "").replace("-", "").lower()
    payload_db = parent.replace("-", "").lower()
    if target_db and payload_db != target_db:
        logger.info(
            "send-faktura: page %s parent DB %s does not match "
            "PROJECTS_DB_ID — skipping",
            page_id,
            parent,
        )
        _record_notion_automation_hit("send-faktura", "wrong_parent_db")
        return _json(
            {"page_id": page_id, "action": "skipped", "reason": "wrong parent DB"}
        )

    inserted = await enqueue_send_faktura(page_id)
    queue_worker.wake()
    action = "queued" if inserted else "already_queued"
    logger.info(
        "📥 send-faktura %s for %s",
        "enqueued" if inserted else "already_queued",
        page_id,
    )
    _record_notion_automation_hit("send-faktura", action)
    return _json({"page_id": page_id, "action": action})


async def _notion_webhook_impl(request: Request) -> Response:
    logger.info("📓 Notion button click received")

    if not _verify_bearer(request.headers.get("Authorization")):
        logger.warning(
            "Notion button auth failed (NOTION_WEBHOOK_SECRET set: %s)",
            bool(settings.notion_webhook_secret),
        )
        raise HTTPException(401, "Bad or missing bearer token")

    raw_body = await request.body()
    try:
        payload: dict[str, Any] = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON body") from None

    # Notion button webhooks wrap the page in `data` (the full page object,
    # including `id`, `parent`, `created_time`, `properties`). We re-fetch the
    # page below to pick up any edits made between the click and our processing,
    # but the envelope is what tells us *which* page to fetch.
    data = payload.get("data") or {}
    page_id = (data.get("id") or "").strip()
    if not page_id:
        raise HTTPException(400, "Missing data.id in Notion button payload")

    try:
        page = await notion_client.get_page(page_id)
    except Exception as err:
        logger.exception("Failed to fetch Notion page %s", page_id)
        raise HTTPException(502, f"Notion fetch failed: {err}") from err

    # Diagnostic: log the parent DB so a misrouted click (e.g. a button on a task
    # row that's set to send the related Prosjekt page) is obvious in the log
    # without guessing which DB the click came from.
    parent_db = (page.get("parent") or {}).get("database_id")
    logger.info("↳ click parent.database_id=%s page=%s", parent_db, page_id)

    # Oppgaver DB takes priority: the per-row "Sync" button on a deliverable
    # row → folder sync (NAS + Frame). The project-DB check below is a `True`
    # no-op if PROJECTS_DB_ID is unset (used as "skip the gate" for dev), so
    # we check Oppgaver first to avoid an Oppgaver-row click being misrouted
    # as a project sync in that mode.
    if _page_parented_to_oppgaver_db(page):
        title = notion_client.extract_page_title(page)
        if not title:
            logger.info("↳ oppgave %s has no title yet, skipping", page_id)
            return _json(
                {
                    "page_id": page_id,
                    "action": "skipped",
                    "reason": "row has no title yet",
                }
            )
        # The Oppgaver DB holds BOTH deliverables and internal tasks,
        # distinguished by `Type`: a real discipline (Interiør/Eksteriør/
        # Animasjon/Annet) is a deliverable and gets Frame + NAS provisioning;
        # any other Type (e.g. "Klargjøre modell") or a blank Type is an
        # internal task — nothing to mirror, skip cleanly.
        discipline = notion_client.task_discipline(page)
        discipline_key = DISCIPLINE_KEYS.get((discipline or "").strip().lower())
        if not discipline_key:
            logger.info(
                "↳ oppgave %s is not a deliverable (type=%r is not a discipline) — skipping",
                page_id,
                discipline,
            )
            return _json(
                {
                    "page_id": page_id,
                    "action": "skipped",
                    "reason": "not a deliverable (Type is not a discipline)",
                    "type": discipline,
                }
            )
        inserted = await enqueue_task_folder_sync(page_id)
        frame_inserted = 0
        if settings.sync_frame:
            # One Notion click fans out to NAS + Frame. Both are independent
            # task_types so a Frame failure can't block NAS retries (and vice
            # versa). The Frame enqueue dedups separately on its own index.
            frame_inserted = await enqueue_frame_leveranse_sync(page_id)
        queue_worker.wake()
        logger.info(
            "📋 deliverable folder sync requested for %r (page %s) — nas=%s frame=%s",
            title,
            page_id,
            "enqueued" if inserted else "already queued",
            ("enqueued" if frame_inserted else "already queued")
            if settings.sync_frame
            else "off",
        )
        return _json(
            {
                "page_id": page_id,
                "action": "queued" if inserted else "already_queued",
                "kind": "task_folder_sync",
                "frame": ("queued" if frame_inserted else "already_queued")
                if settings.sync_frame
                else "off",
            }
        )

    if not _page_parented_to_projects_db(page):
        logger.warning("↳ page %s is not in the Projects DB; rejecting", page_id)
        return _json(
            {
                "page_id": page_id,
                "action": "rejected",
                "reason": "page is not in the configured Projects DB",
            }
        )

    title = notion_client.extract_page_title(page)
    if not title:
        logger.info("↳ page %s has no title yet, skipping", page_id)
        return _json(
            {
                "page_id": page_id,
                "action": "skipped",
                "reason": "page has no title yet",
            }
        )

    # Enqueue + return — do NOT do the Gmail/NAS/Frame work inline. That work
    # is many sequential external API calls; doing it on the request path made
    # this button intermittently exceed Notion's webhook timeout ("failed to
    # execute", button auto-paused). The durable queue worker now runs each
    # task type independently with retries/backoff/visibility, exactly like
    # the resync buttons. Names are recomputed at processing time from the
    # (possibly renamed) title — see each sync engine.
    inserted = await enqueue_label_sync(page_id)
    nas_inserted = 0
    if settings.sync_nas_folders and settings.nas_projects_root:
        # Fan-out: same click also enqueues the NAS provisioner so Initialize
        # still provisions all three systems with one button. Independent
        # task_type means a NAS failure no longer blocks Gmail retries (and a
        # Gmail failure no longer hides a successful NAS run).
        nas_inserted = await enqueue_nas_folder_sync(page_id)
    frame_inserted = 0
    if settings.sync_frame:
        frame_inserted = await enqueue_frame_project_sync(page_id)
    toggl_inserted = 0
    if settings.sync_toggl:
        toggl_inserted = await enqueue_toggl_project_sync(page_id)
    queue_worker.wake()
    logger.info(
        "🏷  initialize requested for %r (page %s) — labels=%s nas=%s frame=%s toggl=%s",
        title,
        page_id,
        "enqueued" if inserted else "already queued",
        ("enqueued" if nas_inserted else "already queued")
        if (settings.sync_nas_folders and settings.nas_projects_root)
        else "off",
        ("enqueued" if frame_inserted else "already queued")
        if settings.sync_frame
        else "off",
        ("enqueued" if toggl_inserted else "already queued")
        if settings.sync_toggl
        else "off",
    )
    return _json(
        {
            "page_id": page_id,
            "action": "queued" if inserted else "already_queued",
            "nas": ("queued" if nas_inserted else "already_queued")
            if (settings.sync_nas_folders and settings.nas_projects_root)
            else "off",
            "frame": ("queued" if frame_inserted else "already_queued")
            if settings.sync_frame
            else "off",
            "toggl": ("queued" if toggl_inserted else "already_queued")
            if settings.sync_toggl
            else "off",
        }
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


def _save_history_cursor_stmt(email: str, history_id: str):
    return (
        pg_insert(SyncCursor)
        .values(source=cursor_source_for(email), cursor_value=str(history_id))
        .on_conflict_do_update(
            index_elements=["source"], set_={"cursor_value": str(history_id)}
        )
    )


async def _save_history_cursor(email: str, history_id: str) -> None:
    async with SessionLocal() as session:
        await session.execute(_save_history_cursor_stmt(email, history_id))
        await session.commit()


async def _save_history_cursor_in_session(session, email: str, history_id: str) -> None:
    """Advance the cursor on a caller-owned transaction (no commit here).

    Used by the Gmail webhook so the enqueue and the cursor advance commit
    together — they can never disagree: either both land or neither does.
    """
    await session.execute(_save_history_cursor_stmt(email, history_id))


async def _known_project_thread_ids(thread_ids: set[str]) -> set[str]:
    """Of the given Gmail thread IDs, return those we've already synced at
    least one message from.

    A new (or SENT) message landing on an already-labeled thread does NOT
    carry the thread's project label in its own labelIds — Gmail labels are
    per-message, and replies/sent mail only get system labels (INBOX/SENT).
    So those threads fall out of the labelIds intersection in
    `_collect_project_thread_ids`. We recover them here: if we've ever synced
    a message from a thread, it's a known project thread and the new message
    belongs in Notion too. Indexed lookup on `EmailRow.gmail_thread_id`; no
    Gmail API call.
    """
    if not thread_ids:
        return set()
    async with SessionLocal() as session:
        rows = await session.execute(
            select(EmailRow.gmail_thread_id).where(
                EmailRow.gmail_thread_id.in_(thread_ids)
            )
        )
        return {row[0] for row in rows}


def _collect_project_thread_ids(
    history_response: dict[str, Any], project_label_ids: set[str]
) -> tuple[set[str], set[str]]:
    """Bucket Gmail history entries into (definite, candidates) thread IDs.

    We re-sync at the thread level (cheap, idempotent via dedup) but only
    when the event involves a project we care about. Two buckets come out:

    `definite` — threads we can confirm belong to a project from the history
    payload alone, no DB needed:
    - messagesAdded whose `labelIds` contains a project label (a labeled
      message landing fresh in the mailbox).
    - labelsAdded whose added `labelIds` intersect our project labels — the
      "user just filed this email into a project" case.
    - labelsRemoved whose removed `labelIds` intersect our project labels.
      Catches "user moved this email from project A to project B": the add
      fires on B, the remove on A, and sync_thread's reconciliation re-points
      existing rows. Without it, swap-only events would silently leave Notion
      rows pointing at the old project.

    `candidates` — threads from messagesAdded whose own `labelIds` did NOT
    match a project label. Gmail labels are per-message: a new reply (or a
    SENT message we ourselves sent) on an already-labeled thread only carries
    system labels (INBOX/SENT/UNREAD), never inheriting the thread's project
    label. We can't tell from the payload whether these belong to a project,
    so the caller resolves them against the local EmailRow cache
    (`_known_project_thread_ids`). Non-project noise (UNREAD toggles on
    unknown threads, CATEGORY_UPDATES, etc.) ends up here and gets dropped by
    that lookup.
    """
    definite: set[str] = set()
    candidates: set[str] = set()
    for entry in history_response.get("history", []) or []:
        for change in entry.get("messagesAdded", []) or []:
            msg = change.get("message") or {}
            tid = msg.get("threadId")
            if not tid:
                continue
            if project_label_ids.intersection(msg.get("labelIds", []) or []):
                definite.add(tid)
            else:
                candidates.add(tid)
        for change_type in ("labelsAdded", "labelsRemoved"):
            for change in entry.get(change_type, []) or []:
                msg = change.get("message") or {}
                tid = msg.get("threadId")
                if not tid:
                    continue
                if project_label_ids.intersection(change.get("labelIds", []) or []):
                    definite.add(tid)
    return definite, candidates


def _history_has_any_changes(history_response: dict[str, Any]) -> bool:
    """True if the history response contains any thread-level change at all,
    regardless of which labels are involved. Used to distinguish "Gmail sent
    us an empty ping" (rare; ack quietly) from "Gmail sent us non-project
    activity" (common; ack quietly and advance the cursor)."""
    for entry in history_response.get("history", []) or []:
        for change_type in ("messagesAdded", "labelsAdded", "labelsRemoved"):
            if entry.get(change_type):
                return True
    return False


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

    # Look up the last historyId we processed for this user.
    last_history_id = await _load_history_cursor(email)
    if not last_history_id:
        # First push for this user — we don't know the starting point yet. Save the
        # incoming one as the baseline so subsequent pushes have something to diff against.
        await _save_history_cursor(email, new_history_id)
        logger.info(
            "📬 Gmail push received for %s: first push, saved historyId=%s as baseline",
            email,
            new_history_id,
        )
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

    # Gmail's watch filter is intentionally coarse (INBOX/SENT) — we can't tighten
    # it (50-label cap vs hundreds of projects/year, and parent labels don't
    # propagate to children in the API). So the first thing we do with every push
    # is filter it against the project labels this mailbox actually cares about.
    # Non-project activity gets silently acked here, with the cursor still
    # advancing so we don't reprocess the same irrelevant history next time.
    project_label_ids = await fetch_project_label_ids_for_user(email)
    definite, candidates = _collect_project_thread_ids(history, project_label_ids)
    # `candidates` are messagesAdded threads whose new message lacked a project
    # label — typically a reply or our own SENT mail on an already-labeled
    # thread. Recover the ones we've synced before from the local cache; the
    # rest is non-project noise and falls away.
    candidates -= definite
    known = await _known_project_thread_ids(candidates)
    thread_ids = definite | known
    if not thread_ids:
        await _save_history_cursor(email, new_history_id)
        if _history_has_any_changes(history):
            logger.debug(
                "Gmail push ignored for %s: no project-label activity since %s",
                email,
                last_history_id,
            )
        else:
            logger.debug(
                "Gmail push ignored for %s: empty history since %s",
                email,
                last_history_id,
            )
        return Response(
            content=json.dumps({"email": email, "synced": [], "errored": []}),
            media_type="application/json",
        )

    logger.info(
        "📬 Gmail push received: email=%s new_historyId=%s — %d project thread(s) to sync (historyId %s → %s)",
        email,
        new_history_id,
        len(thread_ids),
        last_history_id,
        new_history_id,
    )

    # Durable handoff: enqueue the threads AND advance the history cursor in one
    # transaction, then ack Pub/Sub. The actual sync runs later in the queue
    # worker. Doing both in one txn means the cursor can never advance past work
    # that wasn't durably recorded — if the enqueue fails, the cursor stays put
    # and we return non-200 so Pub/Sub redelivers. (Previously this was a
    # fire-and-forget asyncio task that lost everything on a crash.)
    sorted_ids = sorted(thread_ids)
    try:
        async with SessionLocal() as session:
            inserted = await enqueue_threads(email, sorted_ids, session=session)
            await _save_history_cursor_in_session(session, email, new_history_id)
            await session.commit()
    except Exception:
        logger.exception(
            "Enqueue failed for %s — cursor NOT advanced, Pub/Sub will redeliver", email
        )
        raise HTTPException(503, "enqueue failed") from None

    # `inserted` < len when a thread already had an active queue row (deduped) —
    # e.g. a second push/reply on a thread still waiting to be processed.
    skipped = len(sorted_ids) - inserted
    logger.info(
        "  ⇢ enqueued %d thread(s)%s — worker will sync them",
        inserted,
        f" ({skipped} already queued, deduped)" if skipped else "",
    )

    # Wake the worker so it picks the new work up immediately rather than waiting
    # for its poll interval. Best-effort mirror to the client's Notion worklist.
    queue_worker.wake()
    for tid in sorted_ids:
        await queue_mirror.mark_queued(tid, subject=tid)

    return Response(
        content=json.dumps(
            {"email": email, "enqueued": len(sorted_ids), "history_id": new_history_id}
        ),
        media_type="application/json",
    )


# ============================================================
# /webhooks/frame — Frame.io comment events (Phase 2)
# ============================================================
#
# Frame V4 webhook events for comments deliver only `{type, resource:{id,
# type}, account, project, workspace, user}` — no comment body, no author.
# The handler verifies the HMAC, enqueues a `frame_comment_sync` task with
# the resource id, returns 200. The worker drains the queue, fetches the
# full comment from Frame (`GET /comments/{id}?include=owner,replies`),
# and writes it into the Korreksjonsrunde Oppgave's page body.
#
# Verified webhook security spec (Adobe Frame.io developer docs):
#   - Signature header: X-Frameio-Signature, formatted as "v0=<hex>".
#   - Timestamp header: X-Frameio-Request-Timestamp (Unix seconds).
#   - Signing scheme: HMAC-SHA256 over the literal byte string
#     `v0:{timestamp}:{raw_body}` using the webhook's signing secret
#     (returned at registration time as the `secret` field on the webhook
#     object — saved to FRAME_WEBHOOK_SECRET .env).
#   - Replay window: reject requests where |now - timestamp| > 5 minutes.


# Reject requests whose timestamp is too far off — defeats replay attacks.
# 5 minutes matches Frame's own recommendation and Slack's convention.
_FRAME_HMAC_REPLAY_WINDOW_SECONDS = 5 * 60


def _verify_frame_hmac(
    body_bytes: bytes,
    signature_header: str | None,
    timestamp_header: str | None,
) -> bool:
    """Verify a Frame.io webhook signature.

    Signature header looks like `v0=<hex>`. The HMAC is computed over
    the literal string `v0:{timestamp}:{raw_body}` (Slack-style scheme)
    with FRAME_WEBHOOK_SECRET as the key. We also enforce a replay
    window via the timestamp.

    Returns False on any of: missing config, missing headers, malformed
    signature header, replay-window violation, HMAC mismatch.
    """
    if not settings.frame_webhook_secret:
        logger.warning(
            "frame: FRAME_WEBHOOK_SECRET unset — rejecting all webhooks"
        )
        return False
    if not signature_header or not timestamp_header:
        return False
    if not signature_header.startswith("v0="):
        return False

    # Replay-window check. We tolerate a malformed timestamp by rejecting,
    # NOT by treating it as zero — that would let an attacker bypass the
    # window by sending a non-numeric header.
    try:
        ts = int(timestamp_header)
    except ValueError:
        return False
    now = int(time.time())
    if abs(now - ts) > _FRAME_HMAC_REPLAY_WINDOW_SECONDS:
        logger.warning(
            "frame: webhook timestamp %s outside replay window (now=%d, diff=%d)",
            timestamp_header,
            now,
            now - ts,
        )
        return False

    signing_base = f"v0:{timestamp_header}:".encode("utf-8") + body_bytes
    expected = hmac.new(
        settings.frame_webhook_secret.encode("utf-8"),
        signing_base,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature_header[3:], expected)


@router.post("/frame")
async def frame_webhook(request: Request) -> Response:
    """Receive a Frame.io webhook event; enqueue + return 200 fast.

    Wrapped in `request_scope("frame")` so every log line during this
    request (and the queued comment-sync task it spawns) carries the
    `[frame:abcd]` prefix.

    Returns 401 on auth failure (so Frame doesn't retry indefinitely
    with the same bad signature); returns 200 on auth success even when
    the event is one we don't act on, so Frame retires its retry queue
    promptly.
    """
    with request_scope("frame"):
        body_bytes = await request.body()
        sig = request.headers.get("X-Frameio-Signature")
        ts = request.headers.get("X-Frameio-Request-Timestamp")
        if not _verify_frame_hmac(body_bytes, sig, ts):
            logger.warning(
                "frame: webhook auth failed — sig=%r ts=%r body_len=%d",
                sig,
                ts,
                len(body_bytes),
            )
            raise HTTPException(401, "invalid signature")

        try:
            payload = json.loads(body_bytes) if body_bytes else {}
        except json.JSONDecodeError:
            logger.warning("frame: webhook body is not JSON")
            raise HTTPException(400, "body is not JSON") from None

        event_type = payload.get("type") or ""
        resource = payload.get("resource") or {}
        resource_id = resource.get("id")
        resource_type = resource.get("type")

        logger.info(
            "📨 frame webhook %s: resource=%s/%s",
            event_type,
            resource_type,
            resource_id,
        )

        # Phase 2.5 — file.versioned fires when the team drags a new
        # version on top of an existing file. Resource id is the
        # version_stack id (NOT a file id, unlike file.created /
        # file.upload.completed / file.ready). Enqueue and let the
        # engine resolve the stack → Leveranse → flip status to Ferdig.
        if event_type == "file.versioned":
            if not resource_id:
                logger.warning(
                    "frame: file.versioned has no resource.id — ignoring"
                )
                return Response(
                    content=json.dumps({"received": True, "ignored": "no resource.id"}),
                    media_type="application/json",
                )
            inserted = await enqueue_frame_version_sync(resource_id)
            queue_worker.wake()
            logger.info(
                "📨 enqueued frame_version_sync for stack %s (inserted=%d)",
                resource_id,
                inserted,
            )
            return Response(
                content=json.dumps(
                    {
                        "received": True,
                        "event": event_type,
                        "stack_id": resource_id,
                        "enqueued": inserted,
                    }
                ),
                media_type="application/json",
            )

        # Custom metadata field changed on a file (Utgår status mirror).
        # Frame's V4 event name for "a custom field's value changed on a
        # file" is one of `file.status_changed` / `file.metadata_value_changed`
        # / a generic file-update; we accept both known candidates and let
        # the engine no-op when the field is irrelevant. The payload's
        # `resource.id` is the file id; we resolve to a deliverable via
        # the cached placeholder file id and enqueue the reconcile engine
        # with source="frame".
        if event_type in ("file.status_changed", "file.metadata_value_changed"):
            if not resource_id:
                logger.warning(
                    "frame: %s has no resource.id — ignoring", event_type
                )
                return Response(
                    content=json.dumps({"received": True, "ignored": "no resource.id"}),
                    media_type="application/json",
                )
            # Which deliverable does this file back? Two cases:
            #   (a) Direct hit — the event fired on the cached V00
            #       placeholder. Fast path.
            #   (b) Stack hit — the event fired on a V01/V02 file the
            #       team uploaded on top of V00. The placeholder still
            #       lives in the same version stack as a sibling; we
            #       look up by joining the event file's stack-mates
            #       against our cached placeholder ids. Same pattern
            #       sync_frame_stack_audit uses.
            from gb_automations.clients import frame as _frame_client
            from gb_automations.db import SessionLocal as _SessionLocal
            from gb_automations.models import FrameLeveranseFolder as _FLF
            from sqlalchemy import select as _select

            async with _SessionLocal() as session:
                stmt = _select(_FLF).where(
                    _FLF.frame_placeholder_file_id == resource_id
                )
                leveranse_row = (await session.execute(stmt)).scalar_one_or_none()

            if leveranse_row is None:
                # Stack fallback: ask Frame for the file's parent, list
                # stack siblings, and see if any of them is one of our
                # placeholder ids.
                try:
                    file_obj = await _frame_client.get_file(resource_id)
                    parent_id = (
                        file_obj.get("parent_id") if isinstance(file_obj, dict) else None
                    )
                except Exception:  # noqa: BLE001
                    parent_id = None
                sibling_ids: list[str] = []
                if parent_id:
                    try:
                        children = await _frame_client.list_version_stack_children(parent_id)
                    except _frame_client.FrameAPIError as err:
                        if getattr(err, "status_code", None) in (404, 422):
                            children = []
                        else:
                            children = []
                    except Exception:  # noqa: BLE001
                        children = []
                    sibling_ids = [
                        c.get("id") for c in children
                        if isinstance(c, dict) and c.get("id")
                    ]
                if sibling_ids:
                    async with _SessionLocal() as session:
                        stmt = _select(_FLF).where(
                            _FLF.frame_placeholder_file_id.in_(sibling_ids)
                        )
                        leveranse_row = (
                            await session.execute(stmt)
                        ).scalar_one_or_none()

            if leveranse_row is None:
                logger.info(
                    "frame: %s on untracked file %s — skipping",
                    event_type,
                    resource_id,
                )
                return Response(
                    content=json.dumps({"received": True, "ignored": "untracked file"}),
                    media_type="application/json",
                )
            inserted = await enqueue_frame_file_status_sync(
                leveranse_row.notion_page_id, source="frame"
            )
            queue_worker.wake()
            logger.info(
                "📨 enqueued frame_file_status_sync for leveranse %s (source=frame, inserted=%d)",
                leveranse_row.notion_page_id,
                inserted,
            )
            return Response(
                content=json.dumps(
                    {
                        "received": True,
                        "event": event_type,
                        "leveranse_page_id": leveranse_row.notion_page_id,
                        "source": "frame",
                        "enqueued": inserted,
                    }
                ),
                media_type="application/json",
            )

        # Other file.* events (file.created, file.upload.completed,
        # file.ready) fire on EVERY upload including the placeholder
        # that sync_frame_leveranse creates at Initialize. They'd be
        # noisy and require the engine to filter "is this a real
        # version upload" defensively. file.versioned is the precise
        # signal — we ignore the others.
        if not event_type.startswith("comment."):
            logger.info(
                "frame webhook: ignoring %s (only file.versioned + comment.* are acted on)",
                event_type,
            )
            return Response(
                content=json.dumps({"received": True, "ignored": event_type}),
                media_type="application/json",
            )

        if not resource_id:
            logger.warning(
                "frame: comment event %s has no resource.id — ignoring",
                event_type,
            )
            return Response(
                content=json.dumps({"received": True, "ignored": "no resource.id"}),
                media_type="application/json",
            )

        # `comment.deleted` would still match comment.* but there's no
        # value in enqueueing it — we can't fetch a deleted comment to
        # render it as a bullet, and the engine would just self-heal
        # 404 it. Skip explicitly so /debug/queue stays tidy.
        if event_type == "comment.deleted":
            logger.info(
                "frame: comment %s deleted in Frame — no enqueue", resource_id
            )
            return Response(
                content=json.dumps(
                    {"received": True, "ignored": "comment.deleted"}
                ),
                media_type="application/json",
            )

        inserted = await enqueue_frame_comment_sync(resource_id)
        queue_worker.wake()
        logger.info(
            "📨 enqueued frame_comment_sync for %s (event=%s, inserted=%d)",
            resource_id,
            event_type,
            inserted,
        )
        return Response(
            content=json.dumps(
                {
                    "received": True,
                    "event": event_type,
                    "comment_id": resource_id,
                    "enqueued": inserted,
                }
            ),
            media_type="application/json",
        )
