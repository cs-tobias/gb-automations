"""Webhook receivers.

- /webhooks/echo  — logs and reflects whatever it gets; used to verify the
  Cloudflare Tunnel is wired up.
- /webhooks/notion — Notion → Gmail label flow. Triggered by a "Sync to Gmail"
  Notion button on the Projects DB: POST with bearer auth and a JSON body
  {"page_id": "<id>"}. Creates the Gmail label across active mailboxes the
  first time, renames it on later clicks if the project title changed, and
  reconciles per-user rows that drifted.

/webhooks/gmail handles the inbound side (Pub/Sub push from Gmail history).
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token
from googleapiclient.errors import HttpError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import nas as nas_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import EmailRow, ProjectFolder, ProjectLabel, SyncCursor, User
from gb_automations.obs import request_scope
from gb_automations.sync.sync_thread import sync_thread
from gb_automations.sync.watches import cursor_source_for, fetch_project_label_ids_for_user
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


async def _create_label_for_all_users(
    notion_page_id: str, label_name: str
) -> dict[str, list[str]]:
    """Create the label in every active user's mailbox AND record the (project, user) → label_id
    mapping so a future rename can target each label by ID.

    Returns lists of user emails per outcome: `created` minted a fresh label,
    `already_present` was an idempotent no-op (label was already in that
    mailbox), `failed` errored.
    """
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()

    created: list[str] = []
    already_present: list[str] = []
    failed: list[str] = []
    label_ids_by_user: dict[str, str] = {}
    loop = asyncio.get_running_loop()
    for user in users:
        try:
            label = await loop.run_in_executor(
                _executor, partial(gmail_client.create_label, user.email, label_name)
            )
            if label.get("_created"):
                created.append(user.email)
            else:
                already_present.append(user.email)
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

    return {"created": created, "already_present": already_present, "failed": failed}


async def _reconcile_label_for_all_users(
    notion_page_id: str, expected_name: str
) -> dict[str, Any]:
    """Force every active user's Gmail label for this project to match `expected_name`.

    Notion is the source of truth. On every button click we fetch the live label
    name from Gmail (by stored ID), compare to `expected_name`, and patch back
    if they differ. This catches three drift cases in one place:

    1. Project renamed in Notion → DB row is stale → live label still has the
       old name → patch.
    2. Label renamed manually in Gmail → DB row may "look fine" (current_name
       matches expected) but the live label diverged → patch.
    3. Label deleted in Gmail → labels.get returns 404 → create-by-name and
       rewrite the row with the fresh ID (self-heal).

    Returns: {patched: [emails], healed: [emails], unchanged: [emails], failed: [emails]}.
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
        return {"patched": [], "healed": [], "unchanged": [], "failed": [], "no_mapping": True}

    patched: list[str] = []
    healed_ids: dict[str, str] = {}
    unchanged: list[str] = []
    failed: list[str] = []
    loop = asyncio.get_running_loop()

    for row in rows:
        try:
            live = await loop.run_in_executor(
                _executor,
                partial(gmail_client.get_label, row.user_email, row.gmail_label_id),
            )
        except HttpError as err:
            if err.resp.status == 404:
                logger.warning(
                    "Stale label %s for %s (page=%s); creating by name and healing the row",
                    row.gmail_label_id,
                    row.user_email,
                    notion_page_id,
                )
                try:
                    label = await loop.run_in_executor(
                        _executor,
                        partial(gmail_client.create_label, row.user_email, expected_name),
                    )
                    healed_ids[row.user_email] = label["id"]
                    continue
                except Exception:
                    logger.exception(
                        "Self-heal failed for %s (page=%s)", row.user_email, notion_page_id
                    )
                    failed.append(row.user_email)
                    continue
            logger.exception(
                "Failed to fetch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)
            continue
        except Exception:
            logger.exception(
                "Failed to fetch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)
            continue

        if live.get("name") == expected_name:
            unchanged.append(row.user_email)
            continue

        logger.info(
            "↳ drift detected for %s: live=%r expected=%r; patching",
            row.user_email,
            live.get("name"),
            expected_name,
        )
        try:
            await loop.run_in_executor(
                _executor,
                partial(
                    gmail_client.update_label_name,
                    row.user_email,
                    row.gmail_label_id,
                    expected_name,
                ),
            )
            patched.append(row.user_email)
        except Exception:
            logger.exception(
                "Failed to patch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)

    if patched or healed_ids:
        async with SessionLocal() as session:
            touched = patched + list(healed_ids.keys())
            await session.execute(
                update(ProjectLabel)
                .where(ProjectLabel.notion_page_id == notion_page_id)
                .where(ProjectLabel.user_email.in_(touched))
                .values(current_name=expected_name)
            )
            for email, fresh_id in healed_ids.items():
                await session.execute(
                    update(ProjectLabel)
                    .where(ProjectLabel.notion_page_id == notion_page_id)
                    .where(ProjectLabel.user_email == email)
                    .values(gmail_label_id=fresh_id)
                )
            await session.commit()

    return {
        "patched": patched,
        "healed": list(healed_ids.keys()),
        "unchanged": unchanged,
        "failed": failed,
    }


def _json(payload: dict[str, Any], status: int = 200) -> Response:
    return Response(
        content=json.dumps(payload), media_type="application/json", status_code=status
    )


async def _sync_nas_folder_for_project(
    notion_page_id: str, title: str, created_time: str | None
) -> str:
    """Create or rename the project's folder on the office NAS.

    Best-effort and fully decoupled from the Gmail-label step: a down or
    unmounted share (or any filesystem error) is logged and reported, never
    raised — the team relies on label creation, so the NAS must not be able to
    block it. Mirrors how Drive-upload failures are treated in the sync engine.

    Returns one of: "created" | "renamed" | "unchanged" | "skipped" | "failed".
    """
    if not (settings.sync_nas_folders and settings.nas_projects_root):
        return "skipped"

    if not await asyncio.to_thread(nas_client.nas_available):
        logger.warning(
            "↳ NAS unavailable (root %r not a writable dir); skipping folder sync",
            settings.nas_projects_root,
        )
        return "failed"

    try:
        async with SessionLocal() as session:
            row = await session.get(ProjectFolder, notion_page_id)

            if row is None:
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders, title, created_time
                )
                outcome = "created"
            elif row.current_name != title:
                target = await asyncio.to_thread(
                    nas_client.rename_project_folder, row.current_name, title, created_time
                )
                outcome = "renamed"
            else:
                # Unchanged name — still ensure the folder exists, so a folder a
                # user deleted by hand gets healed on the next click.
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders, title, created_time
                )
                outcome = "unchanged"

            await session.merge(
                ProjectFolder(
                    notion_page_id=notion_page_id,
                    current_path=str(target),
                    current_name=title,
                )
            )
            await session.commit()
        logger.info("↳ NAS folder %s: %s", outcome, target)
        return outcome
    except Exception:
        logger.exception("↳ NAS folder sync failed for project %r", title)
        return "failed"


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

    # Year segment of the label path comes from Notion's immutable created_time,
    # so renames only change the leaf — the year stays pinned to the project's
    # creation year even if the title is edited across a year boundary.
    created_time = page.get("created_time")
    label_name = project_label_path(title, created_time)

    # Fan out to every enabled target. Each is independently togglable via
    # config (see Settings.sync_*) so we can decouple while building — e.g. run
    # the NAS step alone during testing with SYNC_GMAIL_LABELS=false.
    parts: list[str] = []

    # --- Gmail label target ---
    if settings.sync_gmail_labels:
        # Two operations on every click, in this order:
        #   1. Reconcile existing rows: compare each user's live Gmail label name
        #      against `label_name` and patch any drift (whether the drift came
        #      from a Notion rename or a Gmail-side rename).
        #   2. Top up missing rows: create the label for any active user who
        #      doesn't yet have a project_labels row (new workspace user, or a
        #      first-ever sync of this project — same code path either way).
        # Order matters: step 1 is a no-op when there are no rows yet (first
        # sync), and step 2's create is idempotent so it doesn't re-create
        # labels users already have.
        reconcile = await _reconcile_label_for_all_users(page_id, label_name)
        topup = await _create_label_for_all_users(page_id, label_name)

        if reconcile.get("no_mapping"):
            gmail_action = "created"
        elif reconcile["patched"] or reconcile["healed"]:
            gmail_action = "synced"
        else:
            gmail_action = "unchanged"

        gmail_failed = list({*reconcile["failed"], *topup["failed"]})

        # The reconcile phase already emits a "↳ drift detected" log when it
        # patches, so the summary here just confirms the outcome and reports any
        # genuine side effect (new label minted, healed, failed).
        if gmail_action == "created":
            parts.append(f"created label {label_name!r} in {len(topup['created'])} mailbox(es)")
        elif reconcile["patched"]:
            parts.append(
                f"renamed label to {label_name!r} in {len(reconcile['patched'])} mailbox(es)"
            )
            if topup["created"]:
                parts.append(f"also created in {len(topup['created'])} new mailbox(es)")
        elif reconcile["healed"]:
            parts.append(
                f"re-created missing label {label_name!r} in {len(reconcile['healed'])} mailbox(es)"
            )
        elif topup["created"]:
            # Reconcile said nothing changed for existing rows, but top-up minted
            # the label somewhere new (e.g. a new active user was added since the
            # last sync of this project).
            parts.append(
                f"created label {label_name!r} in {len(topup['created'])} new mailbox(es)"
            )
        else:
            parts.append(f"label {label_name!r} already up to date everywhere")

        if gmail_failed:
            parts.append(f"label FAILED in {len(gmail_failed)} mailbox(es): {', '.join(gmail_failed)}")
        label_block = {
            "action": gmail_action,
            "patched": reconcile["patched"],
            "healed": reconcile["healed"],
            "unchanged": reconcile["unchanged"],
            "created": topup["created"],
            "already_present": topup["already_present"],
            "failed": gmail_failed,
        }
    else:
        gmail_action = "skipped"
        parts.append("gmail label skipped (SYNC_GMAIL_LABELS=false)")
        label_block = {"action": "skipped"}

    # --- NAS folder target ---
    nas_action = await _sync_nas_folder_for_project(page_id, title, created_time)
    if nas_action != "skipped":
        parts.append(f"NAS folder {nas_action}")

    logger.info("↳ done — %s", "; ".join(parts))

    return _json(
        {
            "page_id": page_id,
            "label": label_name,
            # Top-level `action` reflects the Gmail target for backward compat
            # with the existing Notion button feedback. Per-target detail lives
            # under `gmail` and `nas`.
            "action": gmail_action,
            "gmail": label_block,
            "nas": nas_action,
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
            label = result.thread_subject or "(unknown thread)"
            if result.project_page_id:
                logger.info(
                    "  ✓ %r [%s]: +%d row(s), %d already present",
                    label,
                    result.project_name,
                    result.rows_created,
                    result.rows_already_present,
                )
            else:
                logger.info(
                    "  ⊘ %r skipped: %s",
                    label,
                    result.skipped_reason or "no project match",
                )
            logger.debug("  (thread_id=%s)", tid)
        except Exception:
            logger.exception("sync_thread failed for %s / %s", email, tid)

    try:
        await _save_history_cursor(email, new_history_id)
    except Exception:
        logger.exception("Failed to save history cursor for %s", email)
