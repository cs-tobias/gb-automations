"""Smoke-test routes for Stage 2 — prove the auth chain works for both Gmail and Notion."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import EMAIL_TAGS
from gb_automations.db import SessionLocal
from gb_automations.models import User

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
    """Property names + types on the Emails DB, fresh from Notion (bypasses the cache).

    Use this after renaming a column in Notion to confirm the api sees the new name —
    if it doesn't appear here, the row builder will silently skip writing to it.
    """
    notion_client.reset_schema_cache()
    names = sorted(await notion_client.get_emails_db_property_names())
    return {"count": len(names), "property_names": names}


@router.get("/projects")
async def debug_projects() -> dict[str, Any]:
    """Project name → page ID mapping. Used by Stage 4 (Notion → Gmail label flow)."""
    try:
        projects = await notion_client.get_project_pages()
    except Exception as err:
        raise HTTPException(502, f"Notion call failed: {err}") from err
    return {"count": len(projects), "projects": projects}
