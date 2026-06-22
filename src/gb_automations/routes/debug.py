"""Smoke-test routes for Stage 2 — prove the auth chain works for both Gmail and Notion."""

import re
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


_PROJECT_NUMBER_PREFIX = re.compile(r"^(\d+)_")


def _qualifies_for_sync(name: str) -> bool:
    """A project name qualifies for Toggl sync if it starts with digits
    followed by an underscore, and those digits are >= 1.

    Goldbox's convention: `1238_...`, `657_...`, `003_...` = real billable
    projects. `000_...` = templates, tests, drafts (NOT for time tracking).
    Names without a numeric prefix are also non-billable.
    """
    m = _PROJECT_NUMBER_PREFIX.match(name or "")
    return bool(m) and int(m.group(1)) >= 1


@router.get("/toggl/match-projects")
async def debug_toggl_match_projects() -> dict[str, Any]:
    """Dry-run diagnostic: compare every QUALIFYING Notion project against
    QUALIFYING Toggl projects by name — no writes, no queue tasks.

    A project qualifies when its name starts with a numeric prefix >= 1
    (e.g. `1238_...`, `657_...`, `003_...`). `000_...` and non-numeric
    names are excluded from both sides before matching.

    Returns matched (would adopt), unmatched (would create), and already_cached
    (already linked in the TogglProject table) counts, plus the full lists so
    you can spot name mismatches before running sync-all-projects.
    """
    from gb_automations.clients import notion as notion_client
    from gb_automations.clients import toggl as toggl_client
    from gb_automations.config import settings
    from gb_automations.db import SessionLocal
    from gb_automations.models import TogglProject
    from gb_automations.utils.labels import project_path_parts
    from sqlalchemy import select

    if not settings.toggl_workspace_id:
        return {"action": "skipped", "note": "TOGGL_WORKSPACE_ID not set"}

    notion_projects = await notion_client.get_project_pages()
    toggl_projects = await toggl_client.list_projects(settings.toggl_workspace_id)

    # Filter Toggl side to qualifying names only. Case-insensitive map —
    # keeps in sync with _find_workspace_project_by_name.
    toggl_qualifying: list[dict] = [
        p for p in toggl_projects if _qualifies_for_sync(p.get("name") or "")
    ]
    toggl_by_name: dict[str, dict] = {}
    for p in toggl_qualifying:
        name = (p.get("name") or "").casefold()
        if name and name not in toggl_by_name:
            toggl_by_name[name] = p

    async with SessionLocal() as session:
        cached_ids: set[str] = {
            row.notion_page_id
            for row in (await session.execute(select(TogglProject))).scalars()
        }

    already_cached: list[dict] = []
    matched: list[dict] = []
    unmatched: list[dict] = []
    notion_filtered_out = 0

    for info in notion_projects.values():
        page_id = info.get("id", "")
        title = info.get("title", "")
        created_time = info.get("created_time")
        _year, leaf = project_path_parts(title, created_time)

        if not _qualifies_for_sync(leaf):
            notion_filtered_out += 1
            continue

        entry = {
            "notion_id": page_id,
            "notion_title": title,
            "leaf": leaf,
            "created_time": created_time,
        }

        if page_id in cached_ids:
            already_cached.append(entry)
            continue

        toggl_hit = toggl_by_name.get(leaf.casefold())
        if toggl_hit:
            matched.append({
                **entry,
                "toggl_id": toggl_hit.get("id"),
                "toggl_active": toggl_hit.get("active"),
            })
        else:
            unmatched.append(entry)

    # Sort unmatched by created_time descending (newest first) — old/archived
    # projects in "would_create" are usually expected; recent ones are the
    # interesting cases (real name mismatches that need attention).
    unmatched.sort(key=lambda x: x.get("created_time") or "", reverse=True)
    matched.sort(key=lambda x: x.get("created_time") or "", reverse=True)

    notion_qualifying_count = len(notion_projects) - notion_filtered_out

    return {
        "summary": {
            "filter": "name starts with digits >= 1 followed by '_'",
            "notion_total": len(notion_projects),
            "notion_qualifying": notion_qualifying_count,
            "notion_filtered_out": notion_filtered_out,
            "toggl_total": len(toggl_projects),
            "toggl_qualifying": len(toggl_qualifying),
            "toggl_filtered_out": len(toggl_projects) - len(toggl_qualifying),
            "already_cached": len(already_cached),
            "would_adopt": len(matched),
            "would_create": len(unmatched),
        },
        "would_create_recent_20": unmatched[:20],
        "would_adopt": matched,
        "would_create": unmatched,
        "already_cached": already_cached,
    }


@router.post("/toggl/sync-all-projects")
async def debug_toggl_sync_all_projects() -> dict[str, Any]:
    """Bulk-link every Notion project to its same-name Toggl project — INLINE.

    Single-pass, link-only:
      1. Fetch every Notion project (one paginated call)
      2. Fetch every Toggl project (one paginated `list_projects` — ~7 calls)
      3. Match by name (case-insensitive) in memory
      4. Upsert TogglProject cache rows in a single DB transaction
      5. Never create, rename, or modify anything in Toggl

    This replaces the old per-project queue task design that re-fetched
    the entire Toggl project list for every single project (1400 tasks ×
    7 calls = 10k calls = instant rate-limit). Now it's ~7 Toggl calls
    total no matter how many Notion projects exist.

    Safe to re-run: already-cached rows are updated in place; never
    duplicated.
    """
    from gb_automations.clients import notion as notion_client
    from gb_automations.clients import toggl as toggl_client
    from gb_automations.config import settings
    from gb_automations.db import SessionLocal
    from gb_automations.models import TogglProject
    from gb_automations.utils.labels import project_path_parts
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    if not settings.toggl_workspace_id:
        return {"action": "skipped", "note": "TOGGL_WORKSPACE_ID not set"}

    notion_projects = await notion_client.get_project_pages()
    toggl_projects = await toggl_client.list_projects(settings.toggl_workspace_id)

    # Case-insensitive name → Toggl project lookup (collision = first wins).
    toggl_by_name: dict[str, dict[str, Any]] = {}
    for p in toggl_projects:
        name = (p.get("name") or "").casefold()
        if name and name not in toggl_by_name:
            toggl_by_name[name] = p

    workspace_id = settings.toggl_workspace_id
    rows_to_upsert: list[dict[str, Any]] = []
    matched = 0
    unmatched = 0
    unmatched_samples: list[str] = []

    for info in notion_projects.values():
        page_id = info.get("id", "")
        title = info.get("title", "")
        created_time = info.get("created_time")
        if not page_id or not title:
            continue
        _year, leaf = project_path_parts(title, created_time)
        hit = toggl_by_name.get(leaf.casefold())
        if hit is None:
            unmatched += 1
            if len(unmatched_samples) < 20:
                unmatched_samples.append(leaf)
            continue
        toggl_project_id = str(hit.get("id"))
        rows_to_upsert.append({
            "notion_page_id": page_id,
            "toggl_project_id": toggl_project_id,
            "toggl_workspace_id": workspace_id,
            "current_name": leaf,
            "toggl_url": f"https://track.toggl.com/projects/{toggl_project_id}",
        })
        matched += 1

    # One transaction, upsert all rows. ON CONFLICT updates the mapped
    # fields so a re-run picks up any Toggl-side rename or workspace move.
    if rows_to_upsert:
        async with SessionLocal() as session:
            stmt = pg_insert(TogglProject).values(rows_to_upsert)
            stmt = stmt.on_conflict_do_update(
                index_elements=["notion_page_id"],
                set_={
                    "toggl_project_id": stmt.excluded.toggl_project_id,
                    "toggl_workspace_id": stmt.excluded.toggl_workspace_id,
                    "current_name": stmt.excluded.current_name,
                    "toggl_url": stmt.excluded.toggl_url,
                },
            )
            await session.execute(stmt)
            await session.commit()

    return {
        "action": "ok",
        "notion_total": len(notion_projects),
        "toggl_total": len(toggl_projects),
        "matched_and_cached": matched,
        "unmatched": unmatched,
        "unmatched_samples": unmatched_samples,
        "note": "link-only: nothing was created or modified in Toggl",
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
    nightly 32-day window won't catch up. The engine's reconciliation is
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
            # CSV rows with zero/unparseable Duration. Running entries are
            # already excluded by Toggl's CSV export (no stop time → no
            # Duration), so this counter only fires for malformed rows.
            "zero_duration": result.skipped_zero_duration,
        },
        "kept_without_attribution": {
            # Rows landed in Notion but with an empty Ansatt people-property
            # — informational. The Toggl email doesn't match any active
            # Notion workspace member (ex-employees, freelancers, etc.).
            # The Toggl Bruker navn column still shows who tracked the time.
            "unmatched_user": result.unmatched_user_kept,
            # Rows landed in Notion but with an empty Prosjekt relation —
            # informational. Either the Toggl project isn't mirrored to a
            # Notion page yet, or the entry had no project at all.
            "unmatched_project": result.unmatched_project_kept,
            "no_project": result.no_project_kept,
        },
        "errors": result.errors,
    }


@router.get("/toggl/verify")
async def debug_toggl_verify(
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
) -> dict[str, Any]:
    """Reconcile Toggl truth against the Notion Timer DBs for a window.

    Read-only: pulls Toggl entries + Notion rows, sums on both sides,
    reports total hours, raw entry count, expected aggregated cells
    (= what Notion should hold given the engine's one-row-per-(user,
    project, day) rule), and the diff with a tolerance flag.

    Run after a backfill, or before payroll, to confirm nothing was lost.
    Same Toggl API cost as a backfill on the fetch side (no caching), so
    don't run a 6-year verify casually if rate limits are a concern.

    Query params (both optional, ISO YYYY-MM-DD):
      from — window start (default: Jan 1 of current Oslo year)
      to   — window end   (default: today, Oslo)
    """
    from datetime import date as _date
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo

    from gb_automations.sync.sync_toggl_hours import reconcile_toggl_notion_hours

    _oslo = ZoneInfo("Europe/Oslo")
    today = _dt.now(_oslo).date()
    start = _date.fromisoformat(from_) if from_ else _date(today.year, 1, 1)
    end = _date.fromisoformat(to) if to else today

    return await reconcile_toggl_notion_hours(
        window_start=start,
        window_end=end,
    )


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


@router.get("/notion-automation-health")
async def debug_notion_automation_health() -> dict[str, Any]:
    """Last-seen telemetry for the three Notion-automation receivers.

    Notion auto-pauses webhook automations on perceived failure with no
    publicly visible log on Notion's side — so this endpoint is the
    operator's one-stop check for "are the automations still firing?"
    Returns the in-memory state from routes.webhooks (resets on each
    process restart).

    Each entry shows:
      - `last_seen_utc`: when this automation last hit the receiver (any
         outcome — success, skip, auth fail, bad JSON).
      - `last_action`: the receiver's last response tag (queued /
         already_queued / auth_failed / invalid_json / no_page_id /
         wrong_parent_db). A burst of `auth_failed` here means
         NOTION_WEBHOOK_SECRET drifted between .env and Notion.
      - `count`: per-process call counter so the operator can sanity-check
         "Notion fired this 47 times in the last hour."

    Cheap (one dict read), no auth: it's `/debug/*`, consistent with the
    other debug endpoints, and only ever exposed on the cluster's internal
    network. The information leak (count of recent automation hits) is
    not sensitive.
    """
    # Late import to avoid coupling debug.py to the webhook module at
    # module load — keeps the import graph clean and matches the
    # existing pattern (debug endpoints reach across modules on demand).
    from gb_automations.routes.webhooks import _NOTION_AUTOMATION_LAST_SEEN

    # Return a defensive copy so a concurrent receiver-side update can't
    # race the serialization. The dict is small (3 entries max).
    snapshot = {
        name: dict(entry)
        for name, entry in _NOTION_AUTOMATION_LAST_SEEN.items()
    }
    return {
        "automations": snapshot,
        "note": (
            "Empty dict on a fresh container = no Notion automation has "
            "fired since the last restart. last_action='auth_failed' "
            "repeatedly means NOTION_WEBHOOK_SECRET drift; the receivers "
            "deliberately return 200 on auth failure so Notion doesn't "
            "auto-pause the automation (see plan: stabilise-notion-status-"
            "change-automation-against-auto-pause)."
        ),
    }


# ============================================================
# Fiken — smoke tests + manual draft trigger (bypasses webhook)
# ============================================================


@router.get("/fiken")
async def debug_fiken() -> dict[str, Any]:
    """Fiken connection smoke test.

    Calls `GET /user` + `GET /companies` to prove the full auth chain:
    FIKEN_API_TOKEN valid, Bearer header accepted, Fiken API reachable.
    Echoes back the configured `FIKEN_COMPANY_SLUG` so the operator can
    confirm it matches one of the slugs in the companies list.

    Fails with a 502 + the actual error message if anything is wrong.
    Typical failure: token unset, token revoked, or 99 NOK/mo API module
    not enabled on the Fiken organization.
    """
    from gb_automations.clients import fiken as fiken_client
    from gb_automations.config import settings

    try:
        user = await fiken_client.whoami()
        companies = await fiken_client.list_companies()
    except fiken_client.FikenAPIError as err:
        raise HTTPException(502, str(err)) from err

    company_slugs = [
        (c.get("slug") or "", c.get("name") or "") for c in companies
    ]
    return {
        "ok": True,
        "user_name": user.get("name"),
        "user_email": user.get("email"),
        "companies": company_slugs,
        "configured_slug": settings.fiken_company_slug or None,
        "configured_slug_matches": any(
            slug == settings.fiken_company_slug for slug, _ in company_slugs
        ),
    }


@router.post("/fiken/create-draft")
async def debug_fiken_create_draft(
    project_page_id: str = Query(..., description="Notion Project page id"),
) -> dict[str, Any]:
    """Manually enqueue a send_faktura task — bypasses the Notion button
    (and its bearer auth) so the engine can be exercised from a laptop
    without configuring Notion automations first.

    Same code path as the webhook: enqueue + wake worker + return
    immediately. The worker reads the project's `Faktura status` to
    decide oppstart vs slutt; set it on the project before calling this
    endpoint. Poll `/debug/queue` to see when the task finishes.
    """
    from gb_automations.jobs import queue_worker
    from gb_automations.sync.queue import enqueue_send_faktura

    inserted = await enqueue_send_faktura(project_page_id)
    queue_worker.wake()
    return {
        "project_page_id": project_page_id,
        "action": "queued" if inserted else "already_queued",
    }


@router.post("/fiken/graduate-project")
async def debug_fiken_graduate_project(
    project_page_id: str = Query(
        ..., description="Notion Project page id"
    ),
) -> dict[str, Any]:
    """Phase C.3a — manually graduate one project's sent Fiken invoices.

    Walks the same engine that C.3b's hourly poller will use, but
    scoped to a single project. For each sent Fiken invoice that
    matches this project (via draft_uuid FK or reference.our exact
    match):

      1. Creates a row in the Faktura DB (idempotent via cache).
      2. When matched via draft_uuid (i.e. we have a per-line audit
         trail), graduates Project + Oppgave statuses to the right
         Fakturert* values and clears Faktura handling on both.
      3. Records sent_at + sent_url + invoice_number on the
         FikenInvoice audit row so subsequent calls short-circuit.

    Returns a JSON summary listing every Fiken invoice scanned,
    which matched + how, and what changed in Notion. Safe to
    re-run — all writes idempotent.

    Requires `FAKTURA_DB_ID` to be set for Faktura DB rows to land;
    when unset, the writer returns None per the C.2 dormant-engine
    contract and the summary records `faktura_db_page_id: null`
    (status writes still fire).
    """
    from gb_automations.sync.graduate_project import graduate_project

    summary = await graduate_project(project_page_id)
    return summary.as_dict()


@router.post("/fiken/simulate-graduation")
async def debug_fiken_simulate_graduation(
    project_page_id: str = Query(
        ...,
        description=(
            "Notion project page id. The endpoint reads this project's "
            "most recent UNSENT FikenInvoice row + its FikenInvoiceLine "
            "audit rows and pretends the draft was sent."
        ),
    ),
) -> dict[str, Any]:
    """Phase C.3a — simulate a draft being sent + graduated.

    For testing the full draft→sent→graduate flow without actually
    clicking Send in Fiken (which would create a real Norwegian
    accounting ledger entry).

    Flow:
      1. Find the project's most recent unsent FikenInvoice row +
         its FikenInvoiceLine audit rows (the per-Oppgave NOK).
      2. Fetch the DRAFT from Fiken (read-only) to pull customer +
         reference info for the Faktura DB row.
      3. Build a synthetic "sent invoice" payload: real customer +
         reference + dates from the draft, but with a fake-but-stable
         invoiceNumber/invoiceId so the Faktura DB row is consistent.
      4. Run the full graduation pipeline against this synthetic
         payload:
           - Create the Faktura DB row (idempotent).
           - For each audit-row Oppgave: read current Fakturert beløp,
             add this line's NOK, write the new total + status.
           - Write Project Faktura status to the terminal value.
           - Clear the Faktura utkast URL.
      5. Mark the FikenInvoice row sent_at so the block-check clears.

    Idempotent: re-running graduates the next unsent draft (if any)
    or no-ops (no unsent draft → returns 'no unsent draft for this
    project').

    Read-only against Fiken (just GETs the draft to read payload).
    Writes are to Notion + Postgres only.
    """
    from datetime import datetime, timezone

    from gb_automations.clients import fiken as fiken_client
    from gb_automations.config import (
        FAKTURA_STATUS_50,
        FAKTURA_STATUS_FULL,
        FAKTURERT_STATUS_50,
        FAKTURERT_STATUS_FULL,
        OPPGAVER_PROPS,
        settings,
    )
    from gb_automations.db import SessionLocal
    from gb_automations.models import FikenInvoice, FikenInvoiceLine
    from gb_automations.sync import (
        graduate_project as graduate_engine,
        notion_faktura_db,
    )
    from sqlalchemy import select

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    # --- 1. Read the Notion project ---
    try:
        project_page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        return {"error": f"Failed to read project page: {err}"}
    project_title = (
        notion_client.extract_page_title(project_page) or ""
    ).strip() or None

    # --- 2. Find the most recent unsent FikenInvoice for this project ---
    async with SessionLocal() as session:
        stmt = (
            select(FikenInvoice)
            .where(
                FikenInvoice.company_slug == company_slug,
                FikenInvoice.project_page_id == project_page_id,
                FikenInvoice.sent_at.is_(None),
            )
            .order_by(FikenInvoice.inserted_at.desc())
            .limit(1)
        )
        unsent = (await session.execute(stmt)).scalar_one_or_none()

    if not unsent:
        return {
            "synthetic": True,
            "project_page_id": project_page_id,
            "project_title": project_title,
            "error": (
                "No unsent FikenInvoice row for this project. Click "
                "Send faktura first to create a draft."
            ),
        }

    draft_id = unsent.fiken_invoice_id
    invoice_type = unsent.invoice_type or "slutt"

    # --- 3. Fetch the draft from Fiken to read its real payload ---
    async with await fiken_client._client() as client:
        response = await client.get(
            f"/companies/{company_slug}/invoices/drafts/{draft_id}"
        )
        if response.status_code != 200:
            return {
                "synthetic": True,
                "error": (
                    f"Failed to GET draft {draft_id} from Fiken "
                    f"(status {response.status_code}). Was the draft "
                    f"already deleted?"
                ),
                "body": response.text[:500],
            }
        draft_payload = response.json()

    # --- 4. Synthesize a "sent invoice" payload from the draft ---
    # The Faktura DB writer expects `invoiceId`, `invoiceNumber`, etc.
    # Drafts use `draftId`, no `invoiceNumber`. Build a synthetic dict
    # that has the right keys without faking customer info.
    customer = draft_payload.get("customers") or []
    customer_block: dict[str, Any] = {}
    if customer:
        # Fiken's draft endpoint returns customers as a list; the
        # sent-invoice endpoint returns `customer` as a single dict.
        # Pick the first.
        first = customer[0]
        customer_block = {
            "contactId": first.get("contactId")
            or first.get("customerId"),
            "name": first.get("name") or "",
            "organizationNumber": first.get("organizationNumber"),
        }

    fake_invoice_number = f"SIM-{draft_id}"
    synthetic_invoice: dict[str, Any] = {
        "invoiceId": int(draft_id) if str(draft_id).isdigit() else draft_id,
        "invoiceNumber": fake_invoice_number,
        "issueDate": draft_payload.get("issueDate"),
        "net": draft_payload.get("net"),
        "gross": draft_payload.get("gross"),
        "customer": customer_block,
        "reference": {
            "our": draft_payload.get("ourReference")
            or project_title
            or "",
            "yours": draft_payload.get("yourReference") or "",
        },
        "invoiceText": draft_payload.get("invoiceText") or "",
    }

    # --- 5. Create the Faktura DB row ---
    faktura_db_page_id: str | None = None
    faktura_db_error: str | None = None
    try:
        faktura_db_page_id = await notion_faktura_db.create_faktura_row(
            company_slug,
            fiken_record=synthetic_invoice,
            record_type="faktura",
            project_page_id=project_page_id,
            project_title=project_title,
        )
    except Exception as err:  # noqa: BLE001
        faktura_db_error = f"Faktura DB write failed: {err}"

    # --- 6. Per-Oppgave Fakturert beløp + status writes ---
    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine).where(
            FikenInvoiceLine.company_slug == company_slug,
            FikenInvoiceLine.fiken_invoice_id == draft_id,
        )
        audit_lines = list((await session.execute(stmt)).scalars())

    oppgave_history = (
        FAKTURERT_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURERT_STATUS_FULL
    )
    project_history = (
        FAKTURA_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURA_STATUS_FULL
    )

    oppgave_statuses_set = 0
    oppgave_errors: list[str] = []
    for line in audit_lines:
        if not line.oppgave_page_id:
            continue
        try:
            page = await notion_client.get_page(line.oppgave_page_id)
            current_nok = (
                notion_client.read_number_prop(
                    page, OPPGAVER_PROPS["billed_amount"]
                )
                or 0.0
            )
            new_total = current_nok + (line.billed_amount_ore / 100.0)
            await notion_client.set_oppgave_billed(
                line.oppgave_page_id,
                status=oppgave_history,
                billed_amount_nok=new_total,
            )
            oppgave_statuses_set += 1
        except Exception as err:  # noqa: BLE001
            oppgave_errors.append(
                f"Oppgave {line.oppgave_page_id}: {err}"
            )

    # --- 7. Project status + draft URL clear ---
    project_status_error: str | None = None
    try:
        await notion_client.set_project_faktura_status(
            project_page_id, status=project_history
        )
    except Exception as err:  # noqa: BLE001
        project_status_error = f"set_project_faktura_status: {err}"

    draft_url_error: str | None = None
    try:
        await notion_client.set_project_draft_url(
            project_page_id, url=None
        )
    except Exception as err:  # noqa: BLE001
        draft_url_error = f"set_project_draft_url: {err}"

    # --- 8. Mark the FikenInvoice row sent_at so the block clears ---
    sent_url = graduate_engine._extract_sent_url(
        synthetic_invoice, company_slug
    )
    mark_error: str | None = None
    try:
        await graduate_engine._mark_invoice_sent(
            company_slug=company_slug,
            fiken_invoice_id=draft_id,
            sent_url=sent_url,
            invoice_number=fake_invoice_number,
        )
    except Exception as err:  # noqa: BLE001
        mark_error = f"_mark_invoice_sent: {err}"

    return {
        "synthetic": True,
        "company_slug": company_slug,
        "project_page_id": project_page_id,
        "project_title": project_title,
        "draft_id_graduated": draft_id,
        "invoice_type": invoice_type,
        "synthetic_invoice_number": fake_invoice_number,
        "faktura_db_page_id": faktura_db_page_id,
        "faktura_db_error": faktura_db_error,
        "audit_lines_found": len(audit_lines),
        "oppgave_statuses_set": oppgave_statuses_set,
        "oppgave_errors": oppgave_errors,
        "project_status_set": project_status_error is None,
        "project_status_error": project_status_error,
        "draft_url_cleared": draft_url_error is None,
        "draft_url_error": draft_url_error,
        "audit_marked_sent_at": mark_error is None,
        "mark_error": mark_error,
    }


@router.post("/fiken/graduate-one-invoice")
async def debug_fiken_graduate_one_invoice(
    fiken_invoice_id: str = Query(
        ...,
        description=(
            "Fiken invoice id (numeric, as string) of a real historical "
            "sent invoice in your Fiken account."
        ),
    ),
    project_page_id: str = Query(
        ...,
        description=(
            "Notion project page id to graduate against. The endpoint "
            "treats the invoice AS IF it matched this project — bypasses "
            "the auto-match (draft_uuid / reference.our) logic."
        ),
    ),
    invoice_type: str = Query(
        "oppstart",
        description=(
            "'oppstart' or 'slutt' — drives which terminal status lands "
            "(Fakturert 50% vs Fakturert) on the Project + any Oppgave "
            "with audit rows for this invoice."
        ),
    ),
    mark_sent_in_db: bool = Query(
        False,
        description=(
            "When true, also UPSERT a FikenInvoice row with sent_at=now "
            "so subsequent /debug/fiken/graduate-project runs short-"
            "circuit on this invoice. Default false so the same "
            "historical invoice can be re-tested repeatedly without "
            "DB cleanup."
        ),
    ),
) -> dict[str, Any]:
    """Phase C.3a — graduate ONE historical invoice against a chosen project.

    Designed for safely testing the graduation flow end-to-end without
    creating + sending new Fiken invoices (which dirties the books).
    Pick any real sent invoice from your Fiken account; point it at a
    test Notion project; the endpoint exercises:

      - Faktura DB row write (C.2 path)
      - Project Faktura status flip (Fakturert 50% / Fakturert)
      - Project Faktura utkast URL clear
      - Per-Oppgave Fakturert status flip IF audit rows exist
        (historical invoices won't have any — that case is noted in
        the response and the Oppgave statuses stay unchanged)
      - Idempotency on re-run (cache + read-first/skip-if-same)

    Read-only against Fiken. Every write is to Notion + Postgres,
    both of which you can clean up freely.

    Usage:
        POST /debug/fiken/graduate-one-invoice
            ?fiken_invoice_id=4493258455
            &project_page_id=<notion-id>
            &invoice_type=oppstart
            &mark_sent_in_db=false
    """
    from gb_automations.clients import fiken as fiken_client
    from gb_automations.config import (
        FAKTURA_STATUS_50,
        FAKTURA_STATUS_FULL,
        FAKTURERT_STATUS_50,
        FAKTURERT_STATUS_FULL,
        settings,
    )
    from gb_automations.db import SessionLocal
    from gb_automations.models import FikenInvoiceLine
    from gb_automations.sync import (
        graduate_project as graduate_engine,
        notion_faktura_db,
    )
    from sqlalchemy import select

    if invoice_type not in ("oppstart", "slutt"):
        return {
            "error": (
                f"invoice_type must be 'oppstart' or 'slutt', "
                f"got {invoice_type!r}"
            )
        }

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    # --- 1. Read the Notion project page (presence + title) ---
    try:
        project_page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        return {"error": f"Failed to read project page: {err}"}
    project_title = (
        notion_client.extract_page_title(project_page) or ""
    ).strip() or None

    # --- 2. Fetch the real Fiken invoice payload ---
    async with await fiken_client._client() as client:
        response = await client.get(
            f"/companies/{company_slug}/invoices/{fiken_invoice_id}"
        )
        if response.status_code != 200:
            return {
                "error": f"Fiken returned {response.status_code}",
                "body": response.text[:500],
            }
        fiken_invoice = response.json()

    # --- 3. Write the Faktura DB row (idempotent via C.2 cache) ---
    cached_before = await notion_faktura_db._get_cached_page_id(
        company_slug, "faktura", fiken_invoice_id
    )
    faktura_db_page_id: str | None = None
    faktura_db_error: str | None = None
    try:
        faktura_db_page_id = await notion_faktura_db.create_faktura_row(
            company_slug,
            fiken_record=fiken_invoice,
            record_type="faktura",
            project_page_id=project_page_id,
            project_title=project_title,
        )
    except Exception as err:  # noqa: BLE001
        faktura_db_error = f"Faktura DB write failed: {err}"
    faktura_db_cached = (
        cached_before is not None
        and faktura_db_page_id == cached_before
    )

    # --- 4. Per-Oppgave status flips (only if audit rows exist) ---
    oppgave_history = (
        FAKTURERT_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURERT_STATUS_FULL
    )
    project_history = (
        FAKTURA_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURA_STATUS_FULL
    )

    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine).where(
            FikenInvoiceLine.company_slug == company_slug,
            FikenInvoiceLine.fiken_invoice_id == fiken_invoice_id,
        )
        audit_lines = list((await session.execute(stmt)).scalars())

    # For each Oppgave on the invoice: read current Fakturert beløp,
    # ADD this invoice's billed NOK, write the new total + the status.
    # See _graduate_statuses in sync/graduate_project.py for the
    # rationale (Notion is truth for actually-invoiced money; draft
    # creation doesn't write here, graduation does).
    from gb_automations.config import OPPGAVER_PROPS

    oppgave_statuses_set = 0
    oppgave_errors: list[str] = []
    for line in audit_lines:
        if not line.oppgave_page_id:
            continue
        try:
            page = await notion_client.get_page(line.oppgave_page_id)
            current_nok = (
                notion_client.read_number_prop(
                    page, OPPGAVER_PROPS["billed_amount"]
                )
                or 0.0
            )
            new_total = current_nok + (line.billed_amount_ore / 100.0)
            await notion_client.set_oppgave_billed(
                line.oppgave_page_id,
                status=oppgave_history,
                billed_amount_nok=new_total,
            )
            oppgave_statuses_set += 1
        except Exception as err:  # noqa: BLE001
            oppgave_errors.append(
                f"Oppgave {line.oppgave_page_id}: {err}"
            )

    # --- 5. Project status flip + draft URL clear (always attempt) ---
    project_status_error: str | None = None
    try:
        await notion_client.set_project_faktura_status(
            project_page_id, status=project_history
        )
    except Exception as err:  # noqa: BLE001
        project_status_error = f"set_project_faktura_status: {err}"

    draft_url_error: str | None = None
    try:
        await notion_client.set_project_draft_url(
            project_page_id, url=None
        )
    except Exception as err:  # noqa: BLE001
        draft_url_error = f"set_project_draft_url: {err}"

    # --- 6. Optional: stamp the FikenInvoice audit row as sent ---
    marked_sent: bool = False
    sent_url = graduate_engine._extract_sent_url(
        fiken_invoice, company_slug
    )
    invoice_number = (
        str(fiken_invoice.get("invoiceNumber"))
        if fiken_invoice.get("invoiceNumber") is not None
        else None
    )
    if mark_sent_in_db:
        try:
            await graduate_engine._mark_invoice_sent(
                company_slug=company_slug,
                fiken_invoice_id=fiken_invoice_id,
                sent_url=sent_url,
                invoice_number=invoice_number,
            )
            marked_sent = True
        except Exception as err:  # noqa: BLE001
            return {
                "synthetic": True,
                "error": f"_mark_invoice_sent: {err}",
                "faktura_db_page_id": faktura_db_page_id,
                "faktura_db_cached": faktura_db_cached,
            }

    return {
        "synthetic": True,
        "company_slug": company_slug,
        "project_page_id": project_page_id,
        "project_title": project_title,
        "fiken_invoice_id": fiken_invoice_id,
        "invoice_number": invoice_number,
        "invoice_type": invoice_type,
        "faktura_db_page_id": faktura_db_page_id,
        "faktura_db_cached": faktura_db_cached,
        "faktura_db_error": faktura_db_error,
        "audit_lines_found": len(audit_lines),
        "oppgave_statuses_set": oppgave_statuses_set,
        "oppgave_errors": oppgave_errors,
        "project_status_set": project_status_error is None,
        "project_status_error": project_status_error,
        "draft_url_cleared": draft_url_error is None,
        "draft_url_error": draft_url_error,
        "marked_sent_in_db": marked_sent,
        "sent_url": sent_url,
        "fiken_record_preview": {
            "number": fiken_invoice.get("invoiceNumber"),
            "issueDate": fiken_invoice.get("issueDate"),
            "net": fiken_invoice.get("net"),
            "customer_name": (fiken_invoice.get("customer") or {}).get(
                "name"
            ),
            "reference": fiken_invoice.get("reference")
            or {
                "our": fiken_invoice.get("ourReference"),
                "yours": fiken_invoice.get("yourReference"),
            },
        },
    }


@router.post("/fiken/write-faktura-row")
async def debug_fiken_write_faktura_row(
    fiken_invoice_id: str = Query(
        ..., description="Fiken invoice id (numeric, as a string)"
    ),
    record_type: str = Query(
        "faktura",
        description="'faktura' or 'kreditnota'",
    ),
    project_page_id: str | None = Query(
        None,
        description=(
            "Optional Notion Project page id to link via the Prosjekt "
            "relation. Omit to leave it blank."
        ),
    ),
) -> dict[str, Any]:
    """Phase C.2 — manually write one Faktura DB row from a Fiken record.

    Fetches the Fiken invoice (or credit note) by id, walks the C.2
    writer, and returns the resulting Notion page id (plus any cache
    hit info). Lets operators sanity-check column-by-column formatting
    against Make.com's existing rows before C.3 wires up the poller.

    Usage:
        POST /debug/fiken/write-faktura-row?fiken_invoice_id=4493258455
        POST /debug/fiken/write-faktura-row?fiken_invoice_id=4493258455
            &project_page_id=<notion-id>

    Requires `FAKTURA_DB_ID` to be set in `.env` (writes are dormant
    otherwise — the engine returns None with an INFO log).

    Idempotent: a repeated call for the same Fiken invoice id returns
    the cached page id (no new Notion row).
    """
    from gb_automations.clients import fiken as fiken_client
    from gb_automations.config import settings
    from gb_automations.sync import notion_faktura_db

    if record_type not in ("faktura", "kreditnota"):
        return {
            "error": (
                f"record_type must be 'faktura' or 'kreditnota', "
                f"got {record_type!r}"
            )
        }

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    # Fetch the live Fiken payload. The poller (C.3) will pass these in
    # from the list endpoint instead of fetching one at a time.
    async with await fiken_client._client() as client:
        endpoint = "invoices" if record_type == "faktura" else "creditNotes"
        response = await client.get(
            f"/companies/{company_slug}/{endpoint}/{fiken_invoice_id}"
        )
        if response.status_code != 200:
            return {
                "error": f"Fiken returned {response.status_code}",
                "body": response.text[:500],
            }
        fiken_record = response.json()

    project_title: str | None = None
    if project_page_id:
        try:
            project_page = await notion_client.get_page(project_page_id)
            project_title = (
                notion_client.extract_page_title(project_page) or ""
            ).strip() or None
        except Exception:  # noqa: BLE001
            project_title = None

    new_page_id = await notion_faktura_db.create_faktura_row(
        company_slug,
        fiken_record=fiken_record,
        record_type=record_type,  # type: ignore[arg-type]
        project_page_id=project_page_id,
        project_title=project_title,
    )

    return {
        "company_slug": company_slug,
        "record_type": record_type,
        "fiken_record_id": fiken_invoice_id,
        "notion_page_id": new_page_id,
        "engine_disabled": new_page_id is None
        and not settings.faktura_db_id,
        "fiken_record_preview": {
            "number": fiken_record.get("invoiceNumber")
            or fiken_record.get("creditNoteNumber"),
            "issueDate": fiken_record.get("issueDate"),
            "net": fiken_record.get("net"),
            "customer_name": (fiken_record.get("customer") or {}).get(
                "name"
            ),
            "reference": fiken_record.get("reference")
            or {
                "our": fiken_record.get("ourReference"),
                "yours": fiken_record.get("yourReference"),
            },
        },
    }


@router.get("/fiken/inspect")
async def debug_fiken_inspect(
    project_page_id: str = Query(..., description="Notion Project page id"),
) -> dict[str, Any]:
    """Dump exactly what the Fiken creation engine sees under this project.

    Reads the project's `Faktura status` to determine the run mode
    (oppstart / slutt / skipped), then for each Oppgave returned by
    `oppgaver_for_project` shows every value the eligibility filter
    checks (raw Type, normalized discipline, Pris, Rabatt, Fakturert
    beløp, Fakturert status) and why the row passed or failed. The
    goal is to never have to guess what Notion is sending — if the
    engine says "no eligible Oppgaver", this endpoint tells you which
    check ate them.

    Doesn't enqueue anything, doesn't touch Fiken — pure read.
    """
    from gb_automations.clients import notion as notion_client
    from gb_automations.config import (
        DISCIPLINE_KEYS,
        FAKTURA_STATUS_TO_INVOICE_TYPE,
        FAKTURERT_STATUS_FULL,
        FAKTURERT_STATUS_IKKE,
        FAKTURERT_STATUS_50,
        FAKTURERT_STATUS_UTGAR,
        KORREKSJON_KIND_KORREKSJONSRUNDE,
        OPPGAVER_PROPS,
        PROJECTS_FAKTURA_STATUS_PROP,
    )
    from gb_automations.sync.sync_fiken_invoice import _row_billable_nok

    project_page = await notion_client.get_page(project_page_id)
    faktura_status = notion_client.read_select_name(
        project_page, PROJECTS_FAKTURA_STATUS_PROP
    )
    invoice_type = FAKTURA_STATUS_TO_INVOICE_TYPE.get(faktura_status or "")

    rows = await notion_client.oppgaver_for_project(project_page_id)
    inspected: list[dict[str, Any]] = []

    for row in rows:
        raw_type = notion_client.task_discipline(row)
        canonical = DISCIPLINE_KEYS.get((raw_type or "").strip().lower())
        fakturert_status = (
            notion_client.read_select_name(row, OPPGAVER_PROPS["billed_status"])
            or FAKTURERT_STATUS_IKKE
        )
        price = notion_client.read_number_prop(row, OPPGAVER_PROPS["price_per_row"])
        discount = notion_client.read_number_prop(row, OPPGAVER_PROPS["discount_pct"])
        billed = notion_client.read_number_prop(row, OPPGAVER_PROPS["billed_amount"])

        reasons: list[str] = []
        if (raw_type or "").strip().lower() == KORREKSJON_KIND_KORREKSJONSRUNDE.lower():
            reasons.append("Type = Korreksjonsrunde (Frame.io admin sub-row, never billed)")
        if fakturert_status in (FAKTURERT_STATUS_FULL, FAKTURERT_STATUS_UTGAR):
            reasons.append(f"Fakturert status = {fakturert_status!r}")
        if invoice_type == "oppstart" and fakturert_status != FAKTURERT_STATUS_IKKE:
            reasons.append(
                f"oppstart needs Fakturert status = {FAKTURERT_STATUS_IKKE!r}, "
                f"got {fakturert_status!r}"
            )
        if invoice_type == "slutt" and fakturert_status not in (
            FAKTURERT_STATUS_IKKE,
            FAKTURERT_STATUS_50,
        ):
            reasons.append(f"slutt skips Fakturert status = {fakturert_status!r}")

        billable: tuple[float, float] | None = None
        if invoice_type and not reasons:
            billable = _row_billable_nok(row, invoice_type)
            if billable is None:
                reasons.append("Pris missing or nothing left to bill")

        eligible = not reasons

        title = ""
        for prop in (row.get("properties") or {}).values():
            if prop.get("type") == "title" and prop.get("title"):
                title = "".join(t.get("plain_text", "") for t in prop["title"])
                break

        inspected.append({
            "page_id": row.get("id"),
            "title": title,
            "raw_Type": raw_type,
            "normalized_discipline": canonical,
            "Pris": price,
            "Rabatt_pct": discount,
            "Fakturert_belop": billed,
            "Fakturert_status": fakturert_status,
            "to_bill_nok": billable[0] if billable else None,
            "new_billed_total_nok": billable[1] if billable else None,
            "eligible": eligible,
            "skipped_because": reasons,
            "archived": bool(row.get("archived")),
            "in_trash": bool(row.get("in_trash")),
        })

    return {
        "project_page_id": project_page_id,
        "project_faktura_status": faktura_status,
        "resolved_invoice_type": invoice_type,
        "total_rows": len(rows),
        "eligible_count": sum(1 for r in inspected if r["eligible"]),
        "rows": inspected,
    }


@router.get("/fiken/orphan-drafts")
async def debug_fiken_orphan_drafts(
    project_page_id: str | None = Query(
        None,
        description=(
            "Optional Notion Project page id. When set, only drafts whose "
            "ourReference casefold-matches the project title are returned. "
            "Omit to list every orphan draft in the company."
        ),
    ),
) -> dict[str, Any]:
    """List Fiken drafts that have no active FikenInvoice audit row.

    An orphan is a live, unsent Fiken draft whose numeric draftId has no
    `FikenInvoice` row with sent_at IS NULL — i.e. a draft we created in a
    run that crashed before recording it (the historical duplicate-draft
    bug), or one created by hand in Fiken's UI.

    Read-only. To remove one, call POST /debug/fiken/delete-draft, or
    POST /debug/fiken/reset-project to clear all of a project's drafts.
    """
    from gb_automations.config import settings
    from gb_automations.sync.sync_fiken_invoice import find_orphan_drafts

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    project_title: str | None = None
    if project_page_id:
        try:
            project_page = await notion_client.get_page(project_page_id)
            project_title = (
                notion_client.extract_page_title(project_page) or ""
            ).strip() or None
        except Exception as err:  # noqa: BLE001
            return {"error": f"Failed to read project page: {err}"}

    orphans = await find_orphan_drafts(
        company_slug, project_title=project_title
    )
    return {
        "company_slug": company_slug,
        "project_page_id": project_page_id,
        "project_title": project_title,
        "orphan_count": len(orphans),
        "orphans": orphans,
    }


@router.post("/fiken/delete-draft")
async def debug_fiken_delete_draft(
    draft_id: str = Query(..., description="Fiken numeric draftId to delete"),
) -> dict[str, Any]:
    """Delete one Fiken draft + stamp sent_at on any matching audit row.

    Explicit single-draft removal — use after reviewing
    /debug/fiken/orphan-drafts. Idempotent: a draft Fiken says is already
    gone returns "gone" and still marks the audit row stale.
    """
    from gb_automations.config import settings
    from gb_automations.sync.sync_fiken_invoice import (
        delete_draft_and_mark_stale,
    )

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    outcome = await delete_draft_and_mark_stale(company_slug, draft_id)
    return {"company_slug": company_slug, "draft_id": draft_id, "outcome": outcome}


@router.post("/fiken/reset-project")
async def debug_fiken_reset_project(
    project_page_id: str = Query(..., description="Notion Project page id"),
) -> dict[str, Any]:
    """Wipe all draft-in-flight state for one project.

    For every unsent FikenInvoice row of this project: best-effort DELETE
    the live Fiken draft + stamp sent_at so it drops out of the slutt
    remainder math and the same-mode block check. Does NOT touch Notion
    billing columns — this only clears the phantom draft state that makes
    a cleared-in-Notion project still read as partly billed.

    Use this when a project's draft state is tangled (e.g. duplicate
    orphan drafts from a crashed run) and you want a clean slate before
    re-clicking Send faktura.
    """
    from gb_automations.config import settings
    from gb_automations.sync.sync_fiken_invoice import reset_project_drafts

    company_slug = settings.fiken_company_slug
    if not company_slug:
        return {"error": "FIKEN_COMPANY_SLUG not set"}

    summary = await reset_project_drafts(company_slug, project_page_id)
    return {"company_slug": company_slug, **summary}
