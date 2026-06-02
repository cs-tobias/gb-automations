"""Smoke-test routes for Stage 2 — prove the auth chain works for both Gmail and Notion."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.clients import frame_auth
from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import EMAIL_TAGS
from gb_automations.db import SessionLocal
from gb_automations.models import ContactSignatureImage, User

router = APIRouter(prefix="/debug", tags=["debug"])

# google-api-python-client is sync — run it on a thread to keep the event loop free.
_executor = ThreadPoolExecutor(max_workers=4)


async def _check_user_active(email: str) -> None:
    async with SessionLocal() as session:
        user = await session.get(User, email)
    if not user or not user.active:
        raise HTTPException(404, f"{email} is not an active user in the users table")


def _list_recent_messages(email: str, limit: int) -> list[dict[str, Any]]:
    service = gmail_client.gmail_for(email)
    msgs = (
        service.users()
        .messages()
        .list(userId="me", maxResults=limit, q="in:inbox")
        .execute()
        .get("messages", [])
    )
    out = []
    for ref in msgs:
        full = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="metadata", metadataHeaders=["Subject", "From"])
            .execute()
        )
        headers = {h["name"]: h["value"] for h in full.get("payload", {}).get("headers", [])}
        out.append(
            {
                "id": ref["id"],
                "subject": headers.get("Subject", "(no subject)"),
                "from": headers.get("From", ""),
                "snippet": full.get("snippet", ""),
            }
        )
    return out


def _list_labels(email: str) -> list[dict[str, str]]:
    service = gmail_client.gmail_for(email)
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    return [{"id": label["id"], "name": label["name"], "type": label["type"]} for label in labels]


@router.get("/inbox")
async def debug_inbox(
    email: str = Query(...), limit: int = Query(5, ge=1, le=25)
) -> dict[str, Any]:
    """Latest N inbox messages for a user. Proves DWD impersonation works."""
    await _check_user_active(email)
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        messages = await loop.run_in_executor(
            _executor, partial(_list_recent_messages, email, limit)
        )
    except Exception as err:
        raise HTTPException(502, f"Gmail call failed: {err}") from err
    return {"email": email, "count": len(messages), "messages": messages}


@router.get("/labels")
async def debug_labels(email: str = Query(...)) -> dict[str, Any]:
    """Every label in a user's mailbox. Used later to check label-creation flows."""
    await _check_user_active(email)
    import asyncio

    loop = asyncio.get_running_loop()
    try:
        labels = await loop.run_in_executor(_executor, partial(_list_labels, email))
    except Exception as err:
        raise HTTPException(502, f"Gmail call failed: {err}") from err
    return {"email": email, "count": len(labels), "labels": labels}


@router.get("/notion")
async def debug_notion() -> dict[str, Any]:
    """Pages the Notion integration can see. Proves Notion auth + page-sharing is set up."""
    try:
        pages = await notion_client.search_pages()
    except Exception as err:
        raise HTTPException(502, f"Notion call failed: {err}") from err
    return {
        "count": len(pages),
        "pages": [
            {
                "id": p.get("id"),
                "title": notion_client.extract_page_title(p),
                "object": p.get("object"),
                "url": p.get("url"),
            }
            for p in pages
        ],
    }


@router.get("/users")
async def debug_users() -> dict[str, Any]:
    """List all users in the users table — handy sanity check after seeding."""
    async with SessionLocal() as session:
        result = await session.execute(select(User))
        users = result.scalars().all()
    return {
        "count": len(users),
        "users": [{"email": u.email, "active": u.active} for u in users],
    }


@router.get("/databases")
async def debug_databases() -> dict[str, Any]:
    """List every Notion database the integration can see — paste the right IDs into .env."""
    try:
        dbs = await notion_client.search_databases()
    except Exception as err:
        raise HTTPException(502, f"Notion call failed: {err}") from err
    return {
        "count": len(dbs),
        "databases": [
            {
                "id": db.get("id"),
                "title": notion_client.extract_database_title(db),
                "url": db.get("url"),
            }
            for db in dbs
        ],
    }


@router.get("/llm")
async def debug_llm(prompt: str = Query(..., description="Text to classify")) -> dict[str, Any]:
    """Smoke-test the local LLM: classify `prompt` against the EMAIL_TAGS taxonomy.

    Returns the tags chosen (subset of EMAIL_TAGS) plus the prompt + allowed
    values used. If the call fails, the tags list will be empty and the api
    logs will show why (network, timeout, model not pulled, etc.).
    """
    tags = await llm_client.classify(prompt=prompt, allowed_values=EMAIL_TAGS)
    return {"prompt": prompt, "allowed_tags": EMAIL_TAGS, "tags": tags}


@router.get("/emails-schema")
async def debug_emails_schema() -> dict[str, Any]:
    """Property names on the CURRENT year's Emails DB, fresh from Notion.

    With year-partitioned DBs all schemas should be identical (we create them
    from the same code constant), so reporting the current year's is the most
    useful single answer. Bypasses the cache to surface manual Notion edits.

    Use after renaming a column in Notion to confirm the api sees the new
    name — if it doesn't appear, the row builder will silently skip it.
    """
    from datetime import UTC, datetime

    from gb_automations.clients import notion_emails_db

    notion_client.reset_schema_cache()
    year = datetime.now(UTC).year
    db_id = await notion_emails_db.get_emails_db_for_year(year)
    names = sorted(await notion_client.get_emails_db_property_names(db_id))
    return {"year": year, "db_id": db_id, "count": len(names), "property_names": names}


@router.get("/projects")
async def debug_projects() -> dict[str, Any]:
    """Gmail label path → {id, title, created_time} for every Notion project.

    Keys are the nested label paths ("Prosjekt/<year>/<title>") used in Gmail.
    """
    try:
        projects = await notion_client.get_project_pages()
    except Exception as err:
        raise HTTPException(502, f"Notion call failed: {err}") from err
    return {"count": len(projects), "projects": projects}


@router.get("/frame")
async def debug_frame() -> dict[str, Any]:
    """Frame.io connection smoke test.

    Calls `GET /v4/me` to prove the full auth chain works end-to-end:
    refresh token valid, IMS exchange succeeds, x-api-key recognized, V4
    gateway responds. Returns the authenticated user's basic profile so
    you can confirm it's the right account (petter@goldbox.no for Goldbox).

    Fails with a 502 + the actual error message if anything is wrong —
    typical failures are FRAME_REFRESH_TOKEN missing (run the bootstrap),
    or refresh token revoked (re-run the bootstrap).
    """
    try:
        me = await frame_client.whoami()
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    # Normalize whether the V4 response wraps in {data: …} or returns the
    # object at the top level — the docs aren't 100% consistent and we'd
    # rather not depend on the wrapping shape until we've seen real responses.
    profile = me.get("data") if isinstance(me, dict) and "data" in me else me
    return {
        "ok": True,
        "email": profile.get("email"),
        "name": profile.get("name"),
        "id": profile.get("id"),
    }


@router.get("/frame/workspace")
async def debug_frame_workspace() -> dict[str, Any]:
    """Confirm FRAME_WORKSPACE_ID resolves AND we can enumerate projects in it.

    One-call check: list the configured workspace's Frame Projects. Each
    Notion project will become a top-level entry here (Frame V4's "Active
    Projects" view). Failure typically means FRAME_WORKSPACE_ID is for a
    different account, or the OAuth user doesn't have access to that
    workspace — re-run the bootstrap.
    """
    from gb_automations.config import settings

    if not settings.frame_workspace_id:
        raise HTTPException(
            400,
            "FRAME_WORKSPACE_ID is not set — run the bootstrap "
            "(`python -m gb_automations.scripts.frame_oauth_bootstrap`).",
        )
    if not settings.frame_account_id:
        raise HTTPException(
            400,
            "FRAME_ACCOUNT_ID is not set — run the bootstrap.",
        )
    try:
        projects = await frame_client.list_projects(
            settings.frame_account_id, settings.frame_workspace_id
        )
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {
        "ok": True,
        "workspace_id": settings.frame_workspace_id,
        "project_count": len(projects),
        "projects": [
            {"id": p.get("id"), "name": p.get("name")}
            for p in projects[:25]
        ],
    }


@router.get("/frame/comments/{file_id}")
async def debug_frame_comments(file_id: str) -> dict[str, Any]:
    """[PROBE] Dump the raw comment list for a Frame File.

    Phase 2 probe endpoint — point this at a known placeholder file id (from
    `FrameLeveranseFolder.frame_placeholder_file_id` in the DB, or by
    eyeballing the URL in Frame's UI). The intent is to learn the exact V4
    response shape (field names, nesting) BEFORE writing the comment sync
    engine. Returns the unwrapped payload as-is so the operator can read
    field names off it. Safe to leave in place after Phase 2 ships — useful
    for ad-hoc debugging when a comment doesn't sync.
    """
    try:
        comments = await frame_client.list_comments(file_id)
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {
        "ok": True,
        "file_id": file_id,
        "comment_count": len(comments),
        "comments": comments,
    }


@router.get("/frame/field-definitions")
async def debug_frame_field_definitions() -> dict[str, Any]:
    """Surface the workspace's custom field definitions for operator discovery.

    Used once during setup of the Notion ↔ Frame Utgår status sync: hit
    this endpoint, locate the entry whose name is "Status" (or whatever
    the workspace's review-status custom field is named), copy its `id`
    into `.env` as `FRAME_STATUS_FIELD_ID`, restart the API.
    """
    from gb_automations.config import settings

    try:
        definitions = await frame_client.list_field_definitions()
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {
        "ok": True,
        "count": len(definitions),
        "configured_status_field_id": settings.frame_status_field_id or None,
        "field_definitions": definitions,
    }


@router.get("/frame/file-metadata/{file_id}")
async def debug_frame_file_metadata(file_id: str) -> dict[str, Any]:
    """[PROBE] Dump a Frame File's full payload — useful for inspecting
    where custom field values live (`metadata` array? `field_values`?
    `custom_fields`?) before wiring up the write side.

    Probe-only: returns the raw, unwrapped file payload. Operator-driven —
    not in any normal sync path.
    """
    try:
        file_obj = await frame_client.get_file(file_id)
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {"ok": True, "file_id": file_id, "file": file_obj}


@router.post("/frame/probe-set-status/{leveranse_page_id}/{value}")
async def debug_frame_probe_set_status(
    leveranse_page_id: str, value: str
) -> dict[str, Any]:
    """[PROBE] Try MULTIPLE candidate Frame V4 endpoints for writing a
    custom Status field value, and report the result of each attempt.

    Goal: discover the right URL shape + method without doing N round-
    trips through Claude. Each variant is tried with `api-version:
    experimental` header. `value` is the literal string to set (e.g.
    "Utgår"). Pass the LEVERANSE page id; the helper resolves the
    Frame file id + project id via the cache.

    NOT used by normal sync — purely a one-shot operator-driven probe.
    """
    from sqlalchemy import select as _select

    from gb_automations.clients import frame_auth as _fa
    from gb_automations.config import settings as _settings
    from gb_automations.db import SessionLocal as _Session
    from gb_automations.models import (
        FrameLeveranseFolder as _FLF,
        FrameProjectFolder as _FPF,
    )

    if not _settings.frame_status_field_id:
        raise HTTPException(400, "FRAME_STATUS_FIELD_ID is not configured")

    async with _Session() as session:
        leveranse_row = await session.get(_FLF, leveranse_page_id)
        if leveranse_row is None:
            raise HTTPException(404, "no FrameLeveranseFolder for this page")
        project_row = await session.get(_FPF, leveranse_row.project_page_id)
        if project_row is None:
            raise HTTPException(404, "no FrameProjectFolder for parent project")

    file_id = leveranse_row.frame_placeholder_file_id
    project_id = project_row.frame_project_id
    field_id = _settings.frame_status_field_id

    # Candidate endpoints to try. Each tuple: (method, url, body, note).
    aid = frame_client._account_id()  # noqa: SLF001
    base = frame_client.FRAME_API_BASE
    candidates: list[tuple[str, str, dict[str, Any], str]] = [
        # Current implementation (already failing) — included as the
        # baseline so the report is comparable.
        (
            "POST",
            f"{base}/accounts/{aid}/projects/{project_id}/metadata/values",
            {
                "data": {
                    "file_ids": [file_id],
                    "values": [{"field_definition_id": field_id, "value": value}],
                }
            },
            "current impl — POST project metadata/values",
        ),
        # PATCH instead of POST on the same URL — the search results
        # suggested "PATCH Update metadata across multiple files".
        (
            "PATCH",
            f"{base}/accounts/{aid}/projects/{project_id}/metadata/values",
            {
                "data": {
                    "file_ids": [file_id],
                    "values": [{"field_definition_id": field_id, "value": value}],
                }
            },
            "PATCH on project metadata/values",
        ),
        # PATCH on file directly — Adobe Developer pattern.
        (
            "PATCH",
            f"{base}/accounts/{aid}/files/{file_id}/metadata/values",
            {
                "data": {
                    "values": [{"field_definition_id": field_id, "value": value}],
                }
            },
            "PATCH on file metadata/values",
        ),
        # PATCH on the file with `metadata` as the top-level key (the
        # 422-thread mentioned this was what users tried before being
        # redirected to the project endpoint).
        (
            "PATCH",
            f"{base}/accounts/{aid}/files/{file_id}",
            {
                "data": {
                    "metadata": [
                        {"field_definition_id": field_id, "value": value}
                    ]
                }
            },
            "PATCH on file with `metadata` array",
        ),
        # POST a single field-value resource — REST-y collection write.
        (
            "POST",
            f"{base}/accounts/{aid}/files/{file_id}/metadata_values",
            {
                "data": {
                    "field_definition_id": field_id,
                    "value": value,
                }
            },
            "POST single file metadata_values",
        ),
    ]

    token = await _fa.get_access_token()
    results: list[dict[str, Any]] = []
    async with await frame_client._client(access_token=token) as client:  # noqa: SLF001
        for method, url, body, note in candidates:
            try:
                response = await client.request(
                    method,
                    url,
                    json=body,
                    headers={"api-version": "experimental"},
                )
                snippet = response.text[:500]
                results.append(
                    {
                        "method": method,
                        "url": url,
                        "note": note,
                        "status": response.status_code,
                        "body_snippet": snippet,
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append(
                    {
                        "method": method,
                        "url": url,
                        "note": note,
                        "status": "exception",
                        "body_snippet": str(err)[:500],
                    }
                )

    return {
        "ok": True,
        "leveranse_page_id": leveranse_page_id,
        "file_id": file_id,
        "project_id": project_id,
        "field_id": field_id,
        "value": value,
        "candidates_tried": len(candidates),
        "results": results,
    }


@router.get("/frame/comment-raw/{comment_id}")
async def debug_frame_comment_raw_variants(comment_id: str) -> dict[str, Any]:
    """[PROBE] Fetch one comment trying include= variants (probe-only).

    The default `?include=owner&include=replies` form returns replies but
    no owner. Try comma-separated and bracket-array forms to find the one
    that surfaces `owner`.
    """
    import httpx

    from gb_automations.clients import frame_auth

    token = await frame_auth.get_access_token()
    aid = frame_client._account_id()  # noqa: SLF001
    base = frame_client.FRAME_API_BASE
    headers = {
        "Authorization": f"Bearer {token}",
        "x-api-key": frame_client._client_id(),  # noqa: SLF001
        "Accept": "application/json",
    }
    variants = [
        "",
        "?include=owner",
        "?include=owner,replies",
        "?include[]=owner&include[]=replies",
        "?fields=owner,owner_id,text",
        "?expand=owner",
        "?include=owner&include=replies",
    ]
    out: list[dict[str, Any]] = []
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30.0) as client:
        for q in variants:
            resp = await client.get(f"/accounts/{aid}/comments/{comment_id}{q}")
            try:
                body = resp.json()
                data = body.get("data", body)
                # Show only the keys + owner fields if present, for compactness
                summary = {
                    "keys": sorted(data.keys()) if isinstance(data, dict) else "non-dict",
                    "owner_id": data.get("owner_id") if isinstance(data, dict) else None,
                    "owner": data.get("owner") if isinstance(data, dict) else None,
                }
            except Exception:  # noqa: BLE001
                summary = {"raw": resp.text[:300]}
            out.append({"query": q or "(none)", "status": resp.status_code, "summary": summary})
    return {"ok": True, "comment_id": comment_id, "variants": out}


@router.get("/frame/comment/{comment_id}")
async def debug_frame_comment(comment_id: str) -> dict[str, Any]:
    """[PROBE] Fetch a single Frame comment by id, raw payload.

    The `file/{id}/comments` list view is missing author info; this single-
    comment endpoint may return more fields. Returns the unwrapped JSON
    so the operator can read field names off it.
    """
    try:
        payload = await frame_client.get_comment_raw(comment_id)
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {"ok": True, "comment_id": comment_id, "payload": payload}


@router.get("/frame/webhooks")
async def debug_frame_webhooks() -> dict[str, Any]:
    """[PROBE] List webhooks currently registered against FRAME_WORKSPACE_ID.

    Operator visibility for "is my Phase 2 webhook registered?". Run after
    the bootstrap to confirm the comment.created webhook for
    `{PUBLIC_HUB}/webhooks/frame` shows up here. If duplicates appear (e.g.
    from re-running the bootstrap before the dedup logic landed), the
    operator can clean them up against the Frame API directly.
    """
    from gb_automations.config import settings

    if not settings.frame_workspace_id:
        raise HTTPException(400, "FRAME_WORKSPACE_ID is not set — run the bootstrap.")
    try:
        webhooks = await frame_client.list_webhooks(settings.frame_workspace_id)
    except frame_auth.FrameAuthError as err:
        raise HTTPException(401, str(err)) from err
    except frame_client.FrameAPIError as err:
        raise HTTPException(502, str(err)) from err
    return {
        "ok": True,
        "workspace_id": settings.frame_workspace_id,
        "webhook_count": len(webhooks),
        "webhooks": webhooks,
    }


@router.get("/queue")
async def queue_status() -> dict[str, Any]:
    """Live state of the durable sync queue: counts by status, oldest pending
    age, current in-progress task(s), and recent terminally-failed tasks.

    The authoritative answer to "is this thread synced or not?" \u2014 the Notion
    mirror is a convenience view on top of this.
    """
    from gb_automations.sync.queue import queue_counts

    return await queue_counts()


@router.post("/queue/retry-failed")
async def queue_retry_failed(thread_id: str | None = Query(default=None)) -> dict[str, Any]:
    """Re-run terminally-failed tasks (operator recovery after a fix).

    Flips `failed` rows back to `pending` with a fresh attempt budget and wakes
    the worker. With `?thread_id=...`, retries just that thread; otherwise all
    failed tasks. Backs a future "Retry" button in the Notion Sync Queue mirror.
    """
    from gb_automations.jobs import queue_worker
    from gb_automations.sync.queue import requeue_failed

    requeued = await requeue_failed(thread_id)
    if requeued:
        queue_worker.wake()
    return {"requeued": requeued, "thread_id": thread_id}


@router.get("/signatures")
async def debug_signatures(
    status: str | None = Query(default=None),
    sender: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """Per-contact learned signature-image state, sorted by most-recently updated.

    Filter with `?status=signature` to see only active skips (the interesting
    case for "why isn't this logo uploading?"). Filter with `?sender=x@y.no`
    to scope to one contact when debugging un-learn / re-learn decisions.

    Recovery recipes (not endpoints — direct DB tweaks, see gotchas.md):
      UPDATE contact_signature_images SET status='allowlisted'
        WHERE content_sha1='<sha1>';
      UPDATE contact_signature_images SET status='signature'
        WHERE sender_email='x@y.no' AND content_sha1='<sha1>';
    """
    async with SessionLocal() as session:
        stmt = (
            select(ContactSignatureImage)
            .order_by(ContactSignatureImage.updated_at.desc())
            .limit(500)
        )
        if status:
            stmt = stmt.where(ContactSignatureImage.status == status)
        if sender:
            stmt = stmt.where(ContactSignatureImage.sender_email == sender.lower())
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "sender_email": r.sender_email,
            "content_sha1": r.content_sha1,
            "status": r.status,
            "thread_seen_count": r.thread_seen_count,
            "first_filename": r.first_filename,
            "last_thread_id": r.last_thread_id,
            "first_seen_at": r.first_seen_at.isoformat(),
            "updated_at": r.updated_at.isoformat(),
        }
        for r in rows
    ]


@router.post("/toggl/sync-hours")
async def debug_toggl_sync_hours() -> dict[str, Any]:
    """Manually trigger the Toggl hours aggregator.

    Enqueues a `toggl_hours_sync` task and wakes the worker. Same code
    path the nightly APScheduler job uses — handy for testing without
    waiting until 02:00 Oslo. Singleton dedup means a second click while
    one is in flight is a no-op (the response says so).
    """
    from gb_automations.jobs import queue_worker
    from gb_automations.sync.queue import enqueue_toggl_hours_sync

    inserted = await enqueue_toggl_hours_sync()
    if inserted:
        queue_worker.wake()
    return {
        "action": "queued" if inserted else "already_queued",
        "note": "watch the api logs for 'toggl hours sync done' summary",
    }


@router.post("/toggl/backfill")
async def debug_toggl_backfill(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> dict[str, Any]:
    """One-shot historical backfill — pull Toggl entries over an EXPLICIT
    date range (default: Jan 1 of current year → today, Oslo) and write
    every aggregated cell to Notion.

    Use this on first turn-on (or after any extended downtime) when the
    nightly 14-day window won't catch up. The engine's reconciliation is
    identical to the nightly path, so a wider window just means more rows
    created — re-running the same range is idempotent.

    Runs INLINE (not via the queue) because a backfill is a known one-shot
    operator action with no risk of duplicate concurrent firing. Returns
    the full result for immediate visibility in the curl response, not
    just the log.

    Query params (both optional, ISO YYYY-MM-DD):
      from — window start (default: Jan 1 of current Oslo year)
      to   — window end   (default: today, Oslo)
    """
    from datetime import date as _date
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from gb_automations.sync.sync_toggl_hours import sync_toggl_hours

    _oslo = ZoneInfo("Europe/Oslo")
    today = _dt.now(_oslo).date()
    start = _date.fromisoformat(from_) if from_ else _date(today.year, 1, 1)
    end = _date.fromisoformat(to) if to else today

    result = await sync_toggl_hours(window_start=start, window_end=end)
    return {
        "action": result.action,
        "note": result.note,
        "window": {"start": result.window_start, "end": result.window_end},
        "entries_fetched": result.entries_fetched,
        "cells_aggregated": result.cells_aggregated,
        "rows_created": result.rows_created,
        "rows_updated": result.rows_updated,
        "rows_archived": result.rows_archived,
        "skipped": {
            "running": result.skipped_running,
            "unmatched": result.skipped_unmatched,
            "unknown_project": result.skipped_unknown_project,
            "no_project": result.skipped_no_project,
        },
        "errors": result.errors,
    }


@router.post("/toggl/refresh-users")
async def debug_toggl_refresh_users() -> dict[str, Any]:
    """Refresh both halves of the Toggl→Notion user map without running a sync.

    Pulls the Toggl workspace member list into `toggl_user_cache`, then
    queries Notion's workspace users (`GET /v1/users`) and reports how
    many distinct emails are indexed. Both numbers must be >= 1 before
    the nightly hours sync can write any rows.

    Useful right after onboarding a new employee (added to Toggl AND
    invited to Notion with the same email) to confirm the mapping
    works end-to-end before the next nightly run.
    """
    from gb_automations.clients import notion as notion_client
    from gb_automations.sync.refresh_toggl_users_cache import (
        refresh_toggl_users_cache,
    )

    toggl_result = await refresh_toggl_users_cache()

    notion_users = await notion_client.list_workspace_users()
    notion_emails: set[str] = set()
    for u in notion_users:
        if u.get("type") != "person":
            continue
        email = ((u.get("person") or {}).get("email") or "").strip().lower()
        if email:
            notion_emails.add(email)

    return {
        "toggl_users_cached": toggl_result.total_in_toggl,
        "toggl_added": toggl_result.added,
        "toggl_updated": toggl_result.updated,
        "toggl_removed": toggl_result.removed,
        "toggl_skipped_no_email": toggl_result.skipped_no_email,
        "notion_users_indexed_by_email": len(notion_emails),
    }
