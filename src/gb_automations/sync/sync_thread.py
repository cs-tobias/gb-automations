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

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import drive as drive_client
from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import EMAIL_TAGS, EMAILS_PROPS, settings
from gb_automations.db import SessionLocal
from gb_automations.models import AttachmentFingerprint, ContactCache, EmailRow
from gb_automations.utils.email_cleaning import (
    clean_body,
    extract_signature_block,
    find_attachment_reference_line,
    find_signature_start_line,
    has_quoted_history_hint,
)
from gb_automations.utils.email_splitting import ExtractedMessage, synthetic_message_id
from gb_automations.utils.history_extraction import extract_history_blocks
from gb_automations.utils.participants import (
    company_from_domain,
    extract_name,
    find_sender_email,
    is_internal,
    parse_participant,
    strict_email_or_empty,
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


def _thread_lock_key(thread_id: str) -> int:
    # Built-in hash() is randomized per Python process (PYTHONHASHSEED), so two
    # workers would derive different keys for the same thread. SHA1-derived
    # int64 is stable across processes and across machines.
    digest = hashlib.sha1(thread_id.encode("utf-8")).digest()[:8]
    return int.from_bytes(digest, byteorder="big", signed=True)


async def _acquire_thread_lock(session: AsyncSession, thread_id: str) -> None:
    """Block until this transaction holds the per-thread advisory lock.

    Released automatically on commit/rollback. Two concurrent calls for the
    same thread_id serialize here; calls for different threads run in parallel.
    """
    key = _thread_lock_key(thread_id)
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})


async def sync_thread(user_email: str, thread_id: str) -> SyncResult:
    """Sync one Gmail thread into Notion. See module docstring for behavior."""
    started = time.monotonic()
    result = SyncResult(thread_id=thread_id, project_name=None, project_page_id=None)
    logger.info("🧵 sync_thread start: thread=%s user=%s", thread_id, user_email)

    # 1. Fetch thread + label map from Gmail (sync API, threadpool-wrapped)
    thread = await asyncio.to_thread(gmail_client.get_thread, user_email, thread_id)
    labels = await asyncio.to_thread(gmail_client.list_labels, user_email)
    label_id_to_name = {label["id"]: label["name"] for label in labels}
    result.messages_seen = len(thread.messages)
    logger.info("  • fetched %d message(s) from Gmail", result.messages_seen)

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
    logger.info("  • matched project %r (page=%s)", project_name, project_page_id)

    # Fetch the Emails DB schema once so we only set properties that exist
    # in the user's actual Notion (schemas vary between workspaces).
    emails_db_props = await notion_client.get_emails_db_property_names()

    # 3 + 4 + 5. Use one DB session for the whole thread so cache writes are
    # atomic. The history-reconstruction LLM call needs the session for the
    # first-encounter check, so we open the session before §3.
    async with SessionLocal() as session:
        # 3. Extract pre-thread email history from the first message's quoted
        # content. Deterministic regex parsing — no LLM in this hot path. Runs
        # every sync (content-hashed synthetic IDs make re-runs idempotent;
        # duplicates dedup-hit silently). See utils/history_extraction.py.
        splits = _extract_history_for_thread(thread)
        try:
            # Serialize concurrent syncs of the same thread. Without this, two
            # Gmail pushes that fire ~seconds apart (e.g. self-emails fire one
            # push for INBOX and one for SENT) race past the EmailRow /
            # Notion-side dedup gates and produce duplicate rows.
            await _acquire_thread_lock(session, thread_id)

            contact_ids = await _upsert_thread_contacts(thread, splits, session)
            result.contacts_upserted = len(contact_ids)
            logger.info("  • upserted %d contact(s)", result.contacts_upserted)

            # One tracker per thread — replies re-carry attachments from earlier
            # messages, and we only want to upload each unique-byte attachment
            # once (to the original sender's row).
            thread_tracker = ThreadAttachmentTracker()
            for msg in thread.messages:
                try:
                    created_count, skipped_count = await _sync_message(
                        msg=msg,
                        extracted=splits.get(msg.message_id, []),
                        project_page_id=project_page_id,
                        user_email=user_email,
                        session=session,
                        emails_db_props=emails_db_props,
                        thread_tracker=thread_tracker,
                    )
                    result.rows_created += created_count
                    result.rows_already_present += skipped_count
                except Exception as err:  # one bad message shouldn't kill the thread
                    logger.exception("Failed to sync message %s", msg.message_id)
                    result.errors.append(f"{msg.message_id}: {err}")

            await session.commit()
        except Exception:
            await session.rollback()
            raise

    elapsed = time.monotonic() - started
    logger.info(
        "🧵 sync_thread done in %.1fs: +%d rows, %d already present, %d contact(s)",
        elapsed,
        result.rows_created,
        result.rows_already_present,
        result.contacts_upserted,
    )
    return result


# ============================================================
# Pre-thread history extraction (regex, runs every sync)
# ============================================================


def _extract_history_for_thread(
    thread: gmail_client.GmailThread,
) -> dict[str, list[ExtractedMessage]]:
    """Pull pre-thread email history out of the first message's quoted content.

    Returns `{first_message_id: [ExtractedMessage, ...]}` if extraction yielded
    anything, otherwise `{}`. Regex-based and deterministic — same input always
    produces the same output, content-hashed synthetic IDs make re-runs
    idempotent at the row-creation layer.

    Only the FIRST message of the thread is processed. Subsequent messages in
    the thread are each their own Gmail message and get their own row via the
    regular `_sync_single_message` path; their quoted history is stripped by
    `clean_body` and doesn't need re-extraction.
    """
    if not thread.messages:
        return {}
    first_msg = thread.messages[0]

    if not has_quoted_history_hint(first_msg.plain_body):
        logger.info("  • no quoted-history hint in first message; skipping extraction")
        return {}

    extracted = extract_history_blocks(
        raw_body=first_msg.plain_body,
        parent_subject=first_msg.subject,
        parent_date=first_msg.date,
    )
    if not extracted:
        logger.info("  • no prior emails recovered from first message's body")
        return {}

    logger.info("  • extracted %d prior email(s) from history (regex)", len(extracted))
    return {first_msg.message_id: extracted}


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
    thread: gmail_client.GmailThread,
    splits: dict[str, list[ExtractedMessage]],
    session: AsyncSession,
) -> dict[str, str]:
    """Walk every external participant in the thread, upsert each to Notion Contacts.

    Returns {email: notion_page_id} for every successfully upserted contact.
    Also tries to enrich each sender's contact with a phone number pulled from
    their signature block (matches Apps Script behavior).

    `splits` is the dict produced by `_presplit_forwarded_chains` — for any
    message that was LLM-split, we also extract participants from each inner
    forwarded message so the original (forwarded) senders/recipients get
    upserted as contacts instead of being lost.
    """
    seen: dict[str, dict[str, Any]] = {}  # email → {name, email, phone, company}

    def add_participant(raw: str) -> None:
        parsed = parse_participant(raw)
        if not parsed or is_internal(parsed.email):
            return
        if parsed.email not in seen:
            seen[parsed.email] = {
                "name": parsed.name,
                "email": parsed.email,
                "phone": None,
                "company": company_from_domain(parsed.email),
            }
        elif parsed.name and not seen[parsed.email]["name"]:
            seen[parsed.email]["name"] = parsed.name

    for msg in thread.messages:
        # 1. Collect every distinct external participant from this message's headers.
        for raw in _split_addresses(msg.from_field, msg.to_field, msg.cc_field):
            add_participant(raw)

        # 1b. If this message was a forwarded chain, also walk the extracted
        # inner senders so the original participants land in Contacts.
        for inner in splits.get(msg.message_id, []):
            if inner.from_field:
                add_participant(inner.from_field)

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
    extracted: list[ExtractedMessage],
    project_page_id: str,
    user_email: str,
    session: AsyncSession,
    emails_db_props: set[str],
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Create Notion rows for one Gmail message.

    Always creates one regex-cleaned row for the Gmail message itself (the
    canonical 1:1 mapping). When `extracted` is non-empty (history was
    reconstructed for this message — only happens for the first message of a
    thread on first encounter), ALSO creates one row per extracted prior email.

    Returns `(rows_created, rows_already_present)`.
    """
    # Attribute the top-level message's attachments to whichever email
    # actually sent them. For non-forwarded messages this is a no-op (all
    # decisions land on the forwarder bucket = msg's own row).
    forwarder_decisions, by_synth = _attribute_attachments(msg, extracted)

    # Standalone callers (e.g. `sync_message`) don't pre-build a tracker;
    # create a per-call one. Real thread syncs always pass theirs in.
    if thread_tracker is None:
        thread_tracker = ThreadAttachmentTracker()

    # 1. The regex single-row path runs for every Gmail message.
    created, skipped = await _sync_single_message(
        msg=msg,
        project_page_id=project_page_id,
        user_email=user_email,
        session=session,
        emails_db_props=emails_db_props,
        attachment_decisions=forwarder_decisions,
        thread_tracker=thread_tracker,
    )

    # 2. If history was reconstructed for this message, create rows for the
    # prior emails too. The LLM prompt is configured to extract ONLY the
    # quoted history (not the top-level message), so there's no overlap with
    # the regex row from §1.
    if extracted:
        logger.info(
            "  → msg %s: reconstructing %d prior email(s)",
            msg.message_id,
            len(extracted),
        )
        c, s = await _sync_forwarded_chain(
            parent_msg=msg,
            extracted=extracted,
            project_page_id=project_page_id,
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            attachments_by_synth=by_synth,
            thread_tracker=thread_tracker,
        )
        created += c
        skipped += s

    return (created, skipped)


async def _sync_single_message(
    *,
    msg: gmail_client.GmailMessage,
    project_page_id: str,
    user_email: str,
    session: AsyncSession,
    emails_db_props: set[str],
    attachment_decisions: list[AttachmentDecision] | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Sync a non-forwarded Gmail message into one Notion row.

    `attachment_decisions` is the subset of Gmail attachments attributed to
    THIS row (the forwarder's bucket from `_attribute_attachments`). When
    None, falls back to partitioning the message's own attachments — used by
    the standalone `sync_message` path that doesn't go through history
    extraction.

    Returns `(1, 0)` if a new row was created, `(0, 1)` if it was already there.
    """
    # 1. Local cache hit?
    cached = await session.get(EmailRow, msg.message_id)
    if cached:
        logger.debug("    dedup hit (local cache) for msg %s", msg.message_id)
        return (0, 1)

    # 2. Notion already has a row for this message ID (different user synced it)?
    existing = await notion_client.find_email_row_by_message_id(msg.message_id)
    if existing:
        logger.debug("    dedup hit (Notion query) for msg %s", msg.message_id)
        await _cache_email_row(
            session,
            message_id=msg.message_id,
            thread_id=msg.thread_id,
            notion_page_id=existing["id"],
            seen_by_email=user_email,
        )
        return (0, 1)

    # 3. Compute the cleaned body; skip creating a row if the message has no
    # content of its own AND no real attachments. Typical case: someone forwards
    # a thread with no commentary — their "body" is just signature + forwarded
    # divider (both stripped by clean_body), and the only "attachments" are
    # inline signature logos. The valuable content is the extracted prior
    # messages (history reconstruction handles those).
    cleaned_body = clean_body(msg.plain_body)
    if attachment_decisions is None:
        attachment_decisions = _partition_attachments(msg.plain_body, msg.attachments)
    has_potential_attachments = any(d.upload for d in attachment_decisions)
    if not cleaned_body and not has_potential_attachments:
        logger.info(
            "    ⊘ skipping msg %s: no body content and no real attachments "
            "(signature-only forward; see extracted history)",
            msg.message_id,
        )
        return (0, 0)

    # 4. Create the row.
    logger.info(
        "    📝 row (gmail message) %s: %s",
        msg.message_id,
        _format_extraction_preview(msg.from_field, msg.subject, cleaned_body),
    )
    properties = await _build_email_row_properties(
        msg=msg,
        project_page_id=project_page_id,
        user_email=user_email,
        emails_db_props=emails_db_props,
    )
    created = await notion_client.create_email_row(properties)
    row_id = created["id"]

    # Upload attachments to Drive and set the row's Files property. Per-
    # attachment errors are logged and don't block the rest. Row already
    # exists at this point, so a total upload failure still leaves a valid
    # (file-less) row in Notion.
    if has_potential_attachments and EMAILS_PROPS.get("files") in emails_db_props:
        uploaded = await _upload_attachments(
            parent_msg=msg,
            decisions=attachment_decisions,
            attributed_sender=_addr_to_email(msg.from_field) or user_email,
            user_email=user_email,
            session=session,
            thread_tracker=thread_tracker or ThreadAttachmentTracker(),
        )
        if uploaded:
            try:
                await notion_client.patch_email_row_files(row_id, uploaded)
            except Exception:
                logger.exception(
                    "Failed to set Files property on %s after upload", row_id
                )

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
    return (1, 0)


async def _sync_forwarded_chain(
    *,
    parent_msg: gmail_client.GmailMessage,
    extracted: list[ExtractedMessage],
    project_page_id: str,
    user_email: str,
    session: AsyncSession,
    emails_db_props: set[str],
    attachments_by_synth: dict[str, list[AttachmentDecision]] | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Create one Notion row per LLM-extracted message.

    The LLM emits every distinct author voice — including the outermost
    forwarder's own commentary as its own record — so there's no separate
    "parent row" branch. All rows use content-hash synthetic IDs derived from
    `(from_field, body)` so retries are idempotent even if the LLM produces
    messages in a different order.
    """
    created = 0
    skipped = 0
    attachments_by_synth = attachments_by_synth or {}

    for inner in extracted:
        synth_id = synthetic_message_id(
            parent_msg.message_id, inner.from_field, inner.body
        )
        c, s = await _sync_extracted_message(
            parent_msg=parent_msg,
            inner=inner,
            synthetic_id=synth_id,
            project_page_id=project_page_id,
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            attachment_decisions=attachments_by_synth.get(synth_id, []),
            thread_tracker=thread_tracker,
        )
        created += c
        skipped += s

    return (created, skipped)


async def _sync_extracted_message(
    *,
    parent_msg: gmail_client.GmailMessage,
    inner: ExtractedMessage,
    synthetic_id: str,
    project_page_id: str,
    user_email: str,
    session: AsyncSession,
    emails_db_props: set[str],
    attachment_decisions: list[AttachmentDecision] | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Create a Notion row for one LLM-extracted sub-message.

    `attachment_decisions` are the Gmail attachments attributed to THIS
    historical email (by filename mention in `inner.body`). Bytes still come
    from `parent_msg` — that's the message that physically carries them.

    Returns `(1, 0)` if created, `(0, 1)` if already present (dedup by synthetic_id).
    """
    cached = await session.get(EmailRow, synthetic_id)
    if cached:
        logger.debug("    dedup hit (local cache) for extracted %s", synthetic_id)
        return (0, 1)

    existing = await notion_client.find_email_row_by_message_id(synthetic_id)
    if existing:
        logger.debug("    dedup hit (Notion query) for extracted %s", synthetic_id)
        await _cache_email_row(
            session,
            message_id=synthetic_id,
            thread_id=parent_msg.thread_id,
            notion_page_id=existing["id"],
            seen_by_email=user_email,
        )
        return (0, 1)

    logger.info(
        "    📝 row (history) %s: %s",
        synthetic_id,
        _format_extraction_preview(inner.from_field, inner.subject, inner.body),
    )
    properties = await _build_extracted_row_properties(
        parent_msg=parent_msg,
        inner=inner,
        synthetic_id=synthetic_id,
        project_page_id=project_page_id,
        user_email=user_email,
        emails_db_props=emails_db_props,
    )
    notion_page = await notion_client.create_email_row(properties)
    row_id = notion_page["id"]

    # Upload any attachments attributed to THIS historical email. Bytes live
    # on parent_msg (the forwarder's Gmail message); fingerprint check runs
    # against inner.from_field so a repeating signature gets counted against
    # the original sender, not the forwarder.
    has_uploadable = any(d.upload for d in (attachment_decisions or []))
    if has_uploadable and EMAILS_PROPS.get("files") in emails_db_props:
        inner_sender = _addr_to_email(inner.from_field) or _addr_to_email(
            parent_msg.from_field
        ) or user_email
        uploaded = await _upload_attachments(
            parent_msg=parent_msg,
            decisions=attachment_decisions or [],
            attributed_sender=inner_sender,
            user_email=user_email,
            session=session,
            thread_tracker=thread_tracker or ThreadAttachmentTracker(),
        )
        if uploaded:
            try:
                await notion_client.patch_email_row_files(row_id, uploaded)
            except Exception:
                logger.exception(
                    "Failed to set Files property on extracted row %s", row_id
                )

    blocks = _build_extracted_chat_blocks(inner, user_email)
    if blocks:
        try:
            await notion_client.append_blocks_to_page(row_id, blocks)
        except Exception:
            logger.exception("Failed to append blocks for extracted message %s", synthetic_id)

    await _cache_email_row(
        session,
        message_id=synthetic_id,
        thread_id=parent_msg.thread_id,
        notion_page_id=row_id,
        seen_by_email=user_email,
    )
    return (1, 0)


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


async def _build_email_row_properties(
    *,
    msg: gmail_client.GmailMessage,
    project_page_id: str,
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
    body = clean_body(msg.plain_body)

    return await _assemble_row_props(
        emails_db_props=emails_db_props,
        subject=msg.subject,
        thread_id=msg.thread_id,
        message_id=msg.message_id,
        project_page_id=project_page_id,
        from_name=from_name,
        from_email=from_email,
        is_outgoing=is_outgoing,
        date_iso=msg.date.isoformat(),
        body=body,
    )


async def _build_extracted_row_properties(
    *,
    parent_msg: gmail_client.GmailMessage,
    inner: ExtractedMessage,
    synthetic_id: str,
    project_page_id: str,
    user_email: str,
    emails_db_props: set[str],
) -> dict[str, Any]:
    """Build Notion properties for an LLM-extracted sub-message.

    Same property shape as a regular row — synthetic_id goes into the
    message_id property so dedup queries work unchanged. No attachments
    (extracted segments don't have Gmail attachment metadata).
    """
    # LLM-extracted messages may return just a display name ("Petter Burhol")
    # with no email address. Use strict parsing here — better to leave the
    # Notion email field unset than to write a name into an email-typed prop.
    from_email = strict_email_or_empty(inner.from_field)
    from_name = extract_name(inner.from_field) or from_email or "(unknown)"
    is_outgoing = bool(from_email) and from_email == user_email.lower()

    return await _assemble_row_props(
        emails_db_props=emails_db_props,
        subject=inner.subject,
        thread_id=parent_msg.thread_id,
        message_id=synthetic_id,
        project_page_id=project_page_id,
        from_name=from_name,
        from_email=from_email,
        is_outgoing=is_outgoing,
        date_iso=inner.date.isoformat(),
        body=inner.body,
    )


async def _assemble_row_props(
    *,
    emails_db_props: set[str],
    subject: str,
    thread_id: str,
    message_id: str,
    project_page_id: str,
    from_name: str,
    from_email: str,
    is_outgoing: bool,
    date_iso: str,
    body: str,
) -> dict[str, Any]:
    """Shared property-dict assembly for regular and extracted rows."""
    # Tag classification via LLM. Gated by:
    #   - `settings.tagging_enabled` (ON by default — see config.py)
    #   - body has at least 10 chars (skip "OK"/"Takk!"/"Super" — the LLM
    #     would only return 'annet' on those, wasting a ~1-2s call per row)
    #   - the Tags property actually exists in the user's Notion schema.
    tags: list[str] = []
    if (
        settings.tagging_enabled
        and body
        and len(body) >= 10
        and EMAILS_PROPS["tags"] in emails_db_props
    ):
        # Body-only on purpose: the subject inherits the parent thread's
        # topic ("Fwd: OBOS-Versalen Inngangsparti og korridor") and would
        # leak topic words to every row in the thread, no matter what the
        # row actually says. The body alone is the truth.
        #
        # Sender role lets the prompt apply direction-dependent tags correctly
        # ('leveranse' fires when the studio delivers; 'underlag' when the
        # client sends brief material in; 'korreksjon' depends on who asks
        # for vs gives changes). Falls back to empty hint if from_email is
        # unknown.
        sender_role = ""
        if from_email:
            sender_role = "intern (Goldbox)" if is_internal(from_email) else "ekstern (klient)"
        tags = await llm_client.classify(
            prompt=body[:3000],
            allowed_values=EMAIL_TAGS,
            sender_role=sender_role,
        )
        if tags:
            logger.info("    🏷  tagged: %s", ", ".join(tags))

    props: dict[str, Any] = {}

    def maybe_set(key: str, value: dict[str, Any]) -> None:
        name = EMAILS_PROPS[key]
        if name in emails_db_props:
            props[name] = value

    maybe_set("subject", {"title": [{"text": {"content": (subject or "(no subject)")[:1900]}}]})
    maybe_set("thread_id", {"rich_text": [{"text": {"content": thread_id}}]})
    maybe_set("message_id", {"rich_text": [{"text": {"content": message_id}}]})
    maybe_set("project", {"relation": [{"id": project_page_id}]})
    if from_name:
        maybe_set("from_name", {"rich_text": [{"text": {"content": from_name[:1900]}}]})
    if from_email:
        maybe_set("from_email", {"email": from_email})
    maybe_set("direction", {"select": {"name": "Outgoing" if is_outgoing else "Incoming"}})
    maybe_set("date", {"date": {"start": date_iso}})
    if body:
        maybe_set(
            "body",
            {
                "rich_text": [
                    {"text": {"content": chunk}}
                    for chunk in notion_client.chunk_text(body, NOTION_RICH_TEXT_CHUNK)
                ]
            },
        )
    if tags:
        maybe_set("tags", {"multi_select": [{"name": t} for t in tags]})
    return props


def _build_chat_blocks(msg: gmail_client.GmailMessage, user_email: str) -> list[dict[str, Any]]:
    """Render one Gmail message as a chat-style block group for a Notion page body."""
    from_email = _addr_to_email(msg.from_field)
    from_name = extract_name(msg.from_field) or from_email
    is_outgoing = from_email == user_email.lower()

    body = clean_body(msg.plain_body) or "_(no message body — forwarded or empty)_"
    timestamp = msg.date.strftime("%b %d, %Y · %H:%M UTC")
    return _assemble_chat_blocks(
        from_name=from_name,
        body=body,
        timestamp=timestamp,
        is_outgoing=is_outgoing,
    )


def _build_extracted_chat_blocks(
    inner: ExtractedMessage, user_email: str
) -> list[dict[str, Any]]:
    """Render an LLM-extracted sub-message as a chat-style block group."""
    from_email = strict_email_or_empty(inner.from_field)
    from_name = extract_name(inner.from_field) or from_email or "(unknown)"
    is_outgoing = bool(from_email) and from_email == user_email.lower()
    body = inner.body or "_(no message body)_"
    timestamp = inner.date.strftime("%b %d, %Y · %H:%M UTC")
    return _assemble_chat_blocks(
        from_name=from_name,
        body=body,
        timestamp=timestamp,
        is_outgoing=is_outgoing,
    )


def _assemble_chat_blocks(
    *, from_name: str, body: str, timestamp: str, is_outgoing: bool
) -> list[dict[str, Any]]:
    """Shared block layout for regular and extracted chat-style entries."""

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


def _addr_to_email(field: str) -> str:
    """Get the lowercased email from a "Name <email>" header value."""
    return find_sender_email(field)


def _format_extraction_preview(from_field: str, subject: str, body: str) -> str:
    """One-line log preview of an extracted message — for visibility of what we
    actually wrote to Notion.

    `from='Anne Hansen <a@x.com>' subject='Tilbud' body=189 chars: 'Hei Tobias…'`

    The body preview is the first 80 chars with newlines collapsed to single
    spaces. Used in both regex and LLM extraction paths so the log shows the
    same shape regardless of source.
    """
    body_preview = " ".join((body or "").split())
    if len(body_preview) > 80:
        body_preview = body_preview[:80] + "…"
    return (
        f"from={from_field[:60]!r} "
        f"subject={(subject or '(no subject)')[:60]!r} "
        f"body={len(body or '')} chars: {body_preview!r}"
    )


@dataclass
class AttachmentDecision:
    """Verdict for one Gmail attachment after position-based filtering.

    Used so the upload loop can log which attachments are being skipped and
    why, without losing the original attachment metadata. The position check
    runs synchronously (no bytes needed); the repetition check (which DOES
    need bytes) happens later inside the upload loop.
    """

    attachment: gmail_client.GmailAttachment
    upload: bool
    skip_reason: str = ""


def _body_mentions_filename(body: str, filename: str) -> bool:
    """True if `filename` appears in `body` — as `[image: name]` OR a bare mention.

    Used to attribute attachments on a forwarded Gmail message to the extracted
    historical email that originally sent them. Matches case-insensitively and
    treats the filename as a literal substring (no globbing).
    """
    if not body or not filename:
        return False
    return filename.lower() in body.lower()


def _partition_attachments(
    body: str, attachments: list[gmail_client.GmailAttachment]
) -> list[AttachmentDecision]:
    """Decide which attachments are worth downloading + uploading.

    Position-based skip: if an attachment's `[image: name]` reference in the
    plain-text body sits at or below the signature-start line, it's a
    signature-region decoration → mark `upload=False`. This rule fires
    without needing bytes, so we can skip the Gmail download entirely.

    Repetition-based detection (signature images that repeat across emails)
    is NOT done here — it requires the bytes for content hashing and runs
    inside the upload loop via `_is_repeating_signature_image`.

    Returns one `AttachmentDecision` per input attachment, preserving order.
    """
    if not attachments:
        return []
    signature_line = find_signature_start_line(body)
    out: list[AttachmentDecision] = []
    for att in attachments:
        if signature_line >= 0:
            ref_line = find_attachment_reference_line(body, att.filename)
            if ref_line >= 0 and ref_line >= signature_line:
                out.append(
                    AttachmentDecision(att, upload=False, skip_reason="signature-region")
                )
                continue
        out.append(AttachmentDecision(att, upload=True))
    return out


def _attribute_attachments(
    parent_msg: gmail_client.GmailMessage,
    extracted: list[ExtractedMessage],
) -> tuple[
    list[AttachmentDecision],
    dict[str, list[AttachmentDecision]],
]:
    """Assign each forwarder-level attachment to whichever email actually sent it.

    Forwarded Gmail messages re-carry attachments from the original sender(s).
    The bytes only exist on the top-level (forwarder's) message, but ownership
    belongs to whichever extracted email's body text mentions the filename.
    Attribution rule: oldest extracted email with a body mention wins; if no
    extracted body mentions the file, it stays on the forwarder's row.

    The position-based signature-region check still runs against the forwarder's
    body — those skips are about decorations on the forwarder's own signature,
    not about ownership.

    Returns `(forwarder_decisions, by_synthetic_id)`:
      - forwarder_decisions: AttachmentDecisions owned by the top-level Gmail
        message (signature-region skips + everything not attributed elsewhere).
      - by_synthetic_id: synthetic_id → list of AttachmentDecisions attributed
        to that extracted email.
    """
    all_decisions = _partition_attachments(parent_msg.plain_body, parent_msg.attachments)
    if not extracted:
        return all_decisions, {}

    # Walk extracted oldest-first. history_extraction emits oldest first, but
    # be defensive in case ordering ever changes.
    ordered = sorted(extracted, key=lambda e: e.date)
    by_synth: dict[str, list[AttachmentDecision]] = {}
    forwarder: list[AttachmentDecision] = []
    for decision in all_decisions:
        if not decision.upload:
            # Signature-region skip on the forwarder's body. Keep on forwarder
            # so it's logged once, not re-checked per extracted email.
            forwarder.append(decision)
            continue
        owner_synth: str | None = None
        for inner in ordered:
            # raw_body preserves `[image: foo.png]` markers that clean_body
            # strips. Falls back to body for callers that don't populate it
            # (older tests, future producers).
            mention_body = inner.raw_body or inner.body
            if _body_mentions_filename(mention_body, decision.attachment.filename):
                owner_synth = synthetic_message_id(
                    parent_msg.message_id, inner.from_field, inner.body
                )
                break
        if owner_synth is None:
            forwarder.append(decision)
        else:
            by_synth.setdefault(owner_synth, []).append(decision)
    return forwarder, by_synth


async def _is_repeating_signature_image(
    sender_email: str,
    filename: str,
    content: bytes,
    session: AsyncSession,
) -> bool:
    """True iff this exact image has been seen 2+ times from this sender.

    Implemented as an upsert so concurrent inserts for the same
    (sender_email, content_sha1) can't crash on UniqueViolation — the second
    insert collapses into an UPDATE that bumps seen_count. Mirrors the upsert
    pattern used by ContactCache and EmailRow elsewhere in this module.

    Content hash is sha1 of the bytes. Stable across emails even when Gmail
    renumbers inline image filenames (`image001.png` → `image004.png`).
    """
    sha1 = hashlib.sha1(content).hexdigest()
    sender_key = sender_email.lower()
    stmt = (
        insert(AttachmentFingerprint)
        .values(
            sender_email=sender_key,
            content_sha1=sha1,
            seen_count=1,
            first_filename=(filename or "")[:255],
        )
        .on_conflict_do_update(
            index_elements=["sender_email", "content_sha1"],
            # func.least caps the counter at 10 — no need to grow forever.
            # Explicit updated_at because on_conflict_do_update doesn't trigger
            # SQLAlchemy's ORM-level onupdate hook.
            set_={
                "seen_count": func.least(AttachmentFingerprint.seen_count + 1, 10),
                "updated_at": func.now(),
            },
        )
        .returning(AttachmentFingerprint.seen_count)
    )
    result = await session.execute(stmt)
    return result.scalar_one() >= 2


@dataclass
class ThreadAttachmentTracker:
    """Per-thread memory of attachment bytes we've already uploaded.

    Gmail re-carries attachments on every reply in a thread (the MIME tree
    rebuilds the quoted message). Without this, the same PDF would upload
    once per reply that quoted it. We map each (filename, size) we've seen
    to the sha1 of the bytes we uploaded; if a later message has the same
    (filename, size) AND the same sha1, we know it's a re-carry and skip.

    Filename+size is the cheap pre-filter: if those don't match, we don't
    even need to hash to be sure it's a different attachment. We only hash
    on a (filename, size) collision to confirm the bytes are identical.
    """

    seen: dict[tuple[str, int], str] = field(default_factory=dict)


async def _upload_attachments(
    *,
    parent_msg: gmail_client.GmailMessage,
    decisions: list[AttachmentDecision],
    attributed_sender: str,
    user_email: str,
    session: AsyncSession,
    thread_tracker: ThreadAttachmentTracker,
) -> list[dict[str, str]]:
    """Download + upload each non-signature attachment, return uploaded {name, url} list.

    `parent_msg` is the top-level Gmail message that physically carries the
    attachment bytes. `decisions` is the subset attributed to whichever row is
    being filled (forwarder's own row OR an extracted historical row).
    `attributed_sender` is the email used for repetition-based signature
    detection — for extracted rows that's the historical author, not the
    forwarder.
    `thread_tracker` carries forward attachment-bytes memory across all
    messages in the same thread so we don't re-upload bytes that a reply
    quoted from an earlier message.

    Failures are per-attachment — one Drive error doesn't stop the rest. The
    row already exists in Notion before this is called, so even a total
    upload failure leaves a valid (file-less) row behind.
    """
    uploaded: list[dict[str, str]] = []
    sender = (attributed_sender or "").lower() or user_email.lower()
    for d in decisions:
        if not d.upload:
            logger.info(
                "    ⏏  skip attachment %r: %s", d.attachment.filename, d.skip_reason
            )
            continue
        if not d.attachment.attachment_id:
            # Inline-embedded part (no separate attachment_id) — body MIME
            # part carries the bytes but we don't currently extract them.
            # Rare; usually cid-referenced signature images.
            logger.info(
                "    ⏏  skip attachment %r: inline-embedded, no id", d.attachment.filename
            )
            continue
        try:
            content = await asyncio.to_thread(
                gmail_client.get_attachment_bytes,
                user_email,
                parent_msg.message_id,
                d.attachment.attachment_id,
            )
        except Exception:
            logger.exception(
                "    ✗ Gmail attachment fetch failed for %r", d.attachment.filename
            )
            continue
        if not content:
            logger.warning(
                "    ⏏  skip attachment %r: empty content from Gmail", d.attachment.filename
            )
            continue
        # Thread-level dedup: if this exact (filename, size) → sha1 already
        # got uploaded earlier in the same thread, the current message is
        # just a reply re-carrying the bytes. Attach to the original sender's
        # row only.
        content_sha1 = hashlib.sha1(content).hexdigest()
        tracker_key = (d.attachment.filename, d.attachment.size)
        previous_sha1 = thread_tracker.seen.get(tracker_key)
        if previous_sha1 is not None and previous_sha1 == content_sha1:
            logger.info(
                "    ⏏  skip attachment %r: duplicate-in-thread (already uploaded)",
                d.attachment.filename,
            )
            continue
        if await _is_repeating_signature_image(sender, d.attachment.filename, content, session):
            logger.info(
                "    ⏏  skip attachment %r: repeating signature image (sender=%s)",
                d.attachment.filename,
                sender,
            )
            continue
        try:
            url = await asyncio.to_thread(
                drive_client.upload_attachment,
                user_email,
                settings.attachments_folder_name,
                d.attachment.filename,
                d.attachment.mime_type,
                content,
            )
        except Exception:
            logger.exception(
                "    ✗ Drive upload failed for %r", d.attachment.filename
            )
            continue
        thread_tracker.seen[tracker_key] = content_sha1
        uploaded.append({"name": d.attachment.filename, "url": url})
        logger.info(
            "    📎 uploaded %r (%.1f KB) from %s",
            d.attachment.filename,
            len(content) / 1024,
            sender,
        )
    return uploaded


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
