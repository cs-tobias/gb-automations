"""Sync engine — given a Gmail thread, create matching Notion rows.

Public entrypoint: `sync_thread(user_email, thread_id)`. Returns a SyncResult
describing what happened. Idempotent: re-running on the same thread is safe
(messages already in Notion are skipped via dedup).

Flow per thread:
  1. Fetch thread + per-user labels from Gmail.
  2. Match thread's labels against Notion projects → which project this thread is for.
  3. Walk every external participant in the thread, upsert each as a Contact.
  4. For each message: dedup (local cache → Notion query) → create row + chat-callout body.

All Gmail calls are sync (google-api-python-client). They run inside
asyncio.to_thread so the async event loop stays free.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import EMAILS_PROPS
from gb_automations.db import SessionLocal
from gb_automations.models import ContactCache, EmailRow
from gb_automations.utils.email_cleaning import clean_body, extract_signature_block
from gb_automations.utils.participants import (
    company_from_domain,
    extract_name,
    find_sender_email,
    is_internal,
    parse_participant,
)
from gb_automations.utils.phone import extract_phone

logger = logging.getLogger(__name__)

# Gmail's per-callout text element limit; we chunk longer bodies.
NOTION_RICH_TEXT_CHUNK = 1900


@dataclass
class SyncResult:
    thread_id: str
    project_name: str | None
    project_page_id: str | None
    messages_seen: int = 0
    rows_created: int = 0
    rows_already_present: int = 0
    contacts_upserted: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)


# ============================================================
# Public entrypoint
# ============================================================


async def sync_thread(user_email: str, thread_id: str) -> SyncResult:
    """Sync one Gmail thread into Notion. See module docstring for behavior."""
    result = SyncResult(thread_id=thread_id, project_name=None, project_page_id=None)

    # 1. Fetch thread + label map from Gmail (sync API, threadpool-wrapped)
    thread = await asyncio.to_thread(gmail_client.get_thread, user_email, thread_id)
    labels = await asyncio.to_thread(gmail_client.list_labels, user_email)
    label_id_to_name = {label["id"]: label["name"] for label in labels}
    result.messages_seen = len(thread.messages)

    if not thread.messages:
        result.skipped_reason = "thread has no messages"
        return result

    # 2. Pick the Notion project for this thread
    project_map = await notion_client.get_project_pages()
    thread_label_names = _collect_thread_label_names(thread.messages, label_id_to_name)
    project_name, project_page_id = _pick_project(thread_label_names, project_map)
    result.project_name = project_name
    result.project_page_id = project_page_id
    if not project_page_id:
        result.skipped_reason = (
            f"no Notion project matches any thread label "
            f"(thread labels: {sorted(thread_label_names)})"
        )
        return result

    # Fetch the Emails DB schema once so we only set properties that exist
    # in the user's actual Notion (schemas vary between workspaces).
    emails_db_props = await notion_client.get_emails_db_property_names()

    # 3 + 4. Use one DB session for the whole thread so cache writes are atomic.
    async with SessionLocal() as session:
        try:
            contact_ids = await _upsert_thread_contacts(thread, session)
            result.contacts_upserted = len(contact_ids)

            for msg in thread.messages:
                try:
                    created = await _sync_message(
                        msg=msg,
                        project_page_id=project_page_id,
                        contact_page_id_by_email=contact_ids,
                        user_email=user_email,
                        session=session,
                        emails_db_props=emails_db_props,
                    )
                    if created:
                        result.rows_created += 1
                    else:
                        result.rows_already_present += 1
                except Exception as err:  # one bad message shouldn't kill the thread
                    logger.exception("Failed to sync message %s", msg.message_id)
                    result.errors.append(f"{msg.message_id}: {err}")

            await session.commit()
        except Exception:
            await session.rollback()
            raise

    return result


# ============================================================
# Project matching
# ============================================================


def _collect_thread_label_names(
    messages: list[gmail_client.GmailMessage], label_id_to_name: dict[str, str]
) -> set[str]:
    """Union of label names across every message in the thread."""
    names: set[str] = set()
    for msg in messages:
        for lid in msg.label_ids:
            if lid in label_id_to_name:
                names.add(label_id_to_name[lid])
    return names


def _pick_project(
    thread_label_names: set[str], project_map: dict[str, str]
) -> tuple[str | None, str | None]:
    """Find the FIRST thread-label whose name matches a Notion project name.

    Returns (project_name, project_page_id) or (None, None) if no match.
    Deterministic by sorting candidates alphabetically so the same thread always
    matches the same project even if Gmail returns labels in different order.
    """
    candidates = sorted(thread_label_names & project_map.keys())
    if not candidates:
        return None, None
    name = candidates[0]
    return name, project_map[name]


# ============================================================
# Contacts — extract from the whole thread, then upsert each
# ============================================================


async def _upsert_thread_contacts(
    thread: gmail_client.GmailThread, session: AsyncSession
) -> dict[str, str]:
    """Walk every external participant in the thread, upsert each to Notion Contacts.

    Returns {email: notion_page_id} for every successfully upserted contact.
    Also tries to enrich each sender's contact with a phone number pulled from
    their signature block (matches Apps Script behavior).
    """
    seen: dict[str, dict[str, Any]] = {}  # email → {name, email, phone, company}

    for msg in thread.messages:
        # 1. Collect every distinct external participant from this message.
        for raw in _split_addresses(msg.from_field, msg.to_field, msg.cc_field):
            parsed = parse_participant(raw)
            if not parsed or is_internal(parsed.email):
                continue
            if parsed.email not in seen:
                seen[parsed.email] = {
                    "name": parsed.name,
                    "email": parsed.email,
                    "phone": None,
                    "company": company_from_domain(parsed.email),
                }
            elif parsed.name and not seen[parsed.email]["name"]:
                seen[parsed.email]["name"] = parsed.name

        # 2. If this message's signature has a phone, attach it to the sender's contact.
        sig = extract_signature_block(msg.plain_body)
        if sig:
            phone = extract_phone(sig)
            sender_email = find_sender_email(msg.from_field)
            if phone and sender_email in seen and not seen[sender_email]["phone"]:
                seen[sender_email]["phone"] = phone

    # 3. Upsert each contact via cache → Notion.
    out: dict[str, str] = {}
    for contact in seen.values():
        try:
            page_id = await _upsert_contact(contact, session)
            if page_id:
                out[contact["email"]] = page_id
        except Exception:
            logger.exception("Failed to upsert contact %s", contact["email"])
    return out


def _split_addresses(*fields: str) -> list[str]:
    """Concatenate from/to/cc header values and split on commas."""
    return [chunk for field in fields if field for chunk in field.split(",")]


async def _upsert_contact(contact: dict[str, Any], session: AsyncSession) -> str | None:
    """Find-or-create a contact. Cache first, then Notion query, then create."""
    email = contact["email"]
    name = contact["name"] or contact["email"]
    phone = contact["phone"]
    company = contact["company"]

    # 1. Local cache hit?
    cached = await session.get(ContactCache, email)
    if cached:
        if phone:
            # Best-effort phone enrichment — only if existing has none. We don't
            # know whether Notion already has one without querying, so just try
            # the update; Notion no-ops if the value is the same.
            try:
                contact_obj = await notion_client.find_contact_by_email(email)
                existing_phone = (
                    (contact_obj or {}).get("properties", {}).get("Phone", {}).get("phone_number")
                )
                if not existing_phone:
                    await notion_client.update_contact_phone(cached.notion_page_id, phone)
            except Exception:
                logger.exception("Phone enrichment failed for %s", email)
        return cached.notion_page_id

    # 2. Notion already has this contact (e.g. created in another run)?
    existing = await notion_client.find_contact_by_email(email)
    if existing:
        await _cache_contact(session, email=email, page_id=existing["id"])
        return existing["id"]

    # 3. Create in Notion.
    created = await notion_client.create_contact(
        name=name, email=email, phone=phone, company=company
    )
    page_id = created["id"]
    await _cache_contact(session, email=email, page_id=page_id)
    logger.info("Created contact %s (%s)", name, email)
    return page_id


async def _cache_contact(session: AsyncSession, *, email: str, page_id: str) -> None:
    stmt = (
        insert(ContactCache)
        .values(email=email, notion_page_id=page_id)
        .on_conflict_do_update(index_elements=["email"], set_={"notion_page_id": page_id})
    )
    await session.execute(stmt)


# ============================================================
# Per-message sync
# ============================================================


async def _sync_message(
    *,
    msg: gmail_client.GmailMessage,
    project_page_id: str,
    contact_page_id_by_email: dict[str, str],
    user_email: str,
    session: AsyncSession,
    emails_db_props: set[str],
) -> bool:
    """Create a Notion row for one Gmail message.

    Returns True if a row was created, False if skipped (already in Notion).
    """
    # 1. Local cache hit?
    cached = await session.get(EmailRow, msg.message_id)
    if cached:
        return False

    # 2. Notion already has a row for this message ID (different user synced it)?
    existing = await notion_client.find_email_row_by_message_id(msg.message_id)
    if existing:
        await _cache_email_row(
            session,
            message_id=msg.message_id,
            thread_id=msg.thread_id,
            notion_page_id=existing["id"],
            seen_by_email=user_email,
        )
        return False

    # 3. Create the row.
    properties = _build_email_row_properties(
        msg=msg,
        project_page_id=project_page_id,
        contact_page_id_by_email=contact_page_id_by_email,
        user_email=user_email,
        emails_db_props=emails_db_props,
    )
    created = await notion_client.create_email_row(properties)
    row_id = created["id"]

    # Append the chat-style callout for this message to the row's page body.
    blocks = _build_chat_blocks(msg, user_email)
    if blocks:
        try:
            await notion_client.append_blocks_to_page(row_id, blocks)
        except Exception:
            logger.exception("Failed to append blocks for message %s", msg.message_id)

    await _cache_email_row(
        session,
        message_id=msg.message_id,
        thread_id=msg.thread_id,
        notion_page_id=row_id,
        seen_by_email=user_email,
    )
    return True


async def _cache_email_row(
    session: AsyncSession,
    *,
    message_id: str,
    thread_id: str,
    notion_page_id: str,
    seen_by_email: str | None,
) -> None:
    stmt = (
        insert(EmailRow)
        .values(
            gmail_message_id=message_id,
            gmail_thread_id=thread_id,
            notion_page_id=notion_page_id,
            seen_by_email=seen_by_email,
        )
        .on_conflict_do_update(
            index_elements=["gmail_message_id"],
            set_={"notion_page_id": notion_page_id, "seen_by_email": seen_by_email},
        )
    )
    await session.execute(stmt)


# ============================================================
# Notion property + block builders
# ============================================================


def _build_email_row_properties(
    *,
    msg: gmail_client.GmailMessage,
    project_page_id: str,
    contact_page_id_by_email: dict[str, str],
    user_email: str,
    emails_db_props: set[str],
) -> dict[str, Any]:
    """Build the Notion properties dict for a single-message Emails-DB row.

    Only sets properties that exist in `emails_db_props` (the actual DB schema),
    so the same code works against workspaces with different schemas.
    """
    from_email = _addr_to_email(msg.from_field)
    from_name = extract_name(msg.from_field) or from_email
    is_outgoing = from_email == user_email.lower()

    participant_emails = _collect_participant_emails(msg)
    linked_contact_ids = [
        contact_page_id_by_email[e] for e in participant_emails if e in contact_page_id_by_email
    ]

    preview = _make_preview(msg)
    attachments_text = _summarize_attachments(msg.attachments)

    def maybe_set(key: str, value: dict[str, Any]) -> None:
        name = EMAILS_PROPS[key]
        if name in emails_db_props:
            props[name] = value

    props: dict[str, Any] = {}
    maybe_set("subject", {"title": [{"text": {"content": (msg.subject or "(no subject)")[:1900]}}]})
    maybe_set("thread_id", {"rich_text": [{"text": {"content": msg.thread_id}}]})
    maybe_set("message_id", {"rich_text": [{"text": {"content": msg.message_id}}]})
    maybe_set("project", {"relation": [{"id": project_page_id}]})
    maybe_set("contacts", {"relation": [{"id": cid} for cid in linked_contact_ids]})
    if from_name:
        maybe_set("from_name", {"rich_text": [{"text": {"content": from_name[:1900]}}]})
    if from_email:
        maybe_set("from_email", {"email": from_email})
    maybe_set("direction", {"select": {"name": "Outgoing" if is_outgoing else "Incoming"}})
    maybe_set("date", {"date": {"start": msg.date.isoformat()}})
    if preview:
        maybe_set("preview", {"rich_text": [{"text": {"content": preview[:1900]}}]})
    if attachments_text:
        maybe_set("attachments", {"rich_text": [{"text": {"content": attachments_text[:1900]}}]})
    return props


def _build_chat_blocks(msg: gmail_client.GmailMessage, user_email: str) -> list[dict[str, Any]]:
    """Render one Gmail message as a chat-style block group for a Notion page body."""
    from_email = _addr_to_email(msg.from_field)
    from_name = extract_name(msg.from_field) or from_email
    is_outgoing = from_email == user_email.lower()

    body = clean_body(msg.plain_body) or "_(no message body — forwarded or empty)_"
    timestamp = msg.date.strftime("%b %d, %Y · %H:%M UTC")

    blocks: list[dict[str, Any]] = []

    # Timestamp line (small gray text above the bubble)
    blocks.append(
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {
                        "type": "text",
                        "text": {"content": timestamp},
                        "annotations": {"italic": True, "color": "gray"},
                    }
                ]
            },
        }
    )

    # Callout with sender + body
    body_chunks = notion_client.chunk_text(body, NOTION_RICH_TEXT_CHUNK)
    blocks.append(
        notion_client.callout_block(
            title=from_name,
            body=body_chunks[0],
            icon="💬" if is_outgoing else "📨",
            color="blue_background" if is_outgoing else "gray_background",
        )
    )
    # If the body overflowed the 1900-char chunk, append the rest as plain paragraphs.
    for chunk in body_chunks[1:]:
        blocks.append(notion_client.paragraph_block(chunk))

    return blocks


# ============================================================
# Small helpers
# ============================================================


def _collect_participant_emails(msg: gmail_client.GmailMessage) -> list[str]:
    """Lowercased unique external participant emails from a single message (from/to/cc)."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in _split_addresses(msg.from_field, msg.to_field, msg.cc_field):
        parsed = parse_participant(raw)
        if not parsed or is_internal(parsed.email):
            continue
        if parsed.email in seen:
            continue
        seen.add(parsed.email)
        out.append(parsed.email)
    return out


def _addr_to_email(field: str) -> str:
    """Get the lowercased email from a "Name <email>" header value."""
    return find_sender_email(field)


def _make_preview(msg: gmail_client.GmailMessage) -> str:
    """One-line preview from the cleaned body — empty if nothing to show."""
    cleaned = clean_body(msg.plain_body)
    if not cleaned:
        return ""
    for line in cleaned.split("\n"):
        if line.strip():
            return line[:200]
    return ""


def _summarize_attachments(attachments: list[gmail_client.GmailAttachment]) -> str:
    """Text summary of attachments for the row's Attachments property.

    Attachment storage is deferred — for Stage 3 we just record the count and
    filenames so the user can see what's attached without leaving Notion. The
    files themselves stay in Gmail until a later stage adds Drive/Notion upload.
    """
    if not attachments:
        return ""
    names = ", ".join(att.filename for att in attachments)
    return f"{len(attachments)} attachment(s): {names}"


# ============================================================
# Convenience: sync a single message (lets the CLI / future webhook target one msg)
# ============================================================


async def sync_message(user_email: str, message_id: str) -> SyncResult:
    """Sync just one Gmail message. Looks up the thread, then delegates to sync_thread.

    Currently a thin wrapper; later (Stage 5 Pub/Sub) we may want a more granular
    path that doesn't re-walk the whole thread.
    """
    msg = await asyncio.to_thread(gmail_client.get_message, user_email, message_id)
    return await sync_thread(user_email, msg.thread_id)


__all__ = [
    "SyncResult",
    "sync_message",
    "sync_thread",
]


# Re-export for type-checkers / readers
_ = datetime  # silence unused-import linter; datetime is used via msg.date
