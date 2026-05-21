"""Sync engine — given a Gmail thread, create matching Notion rows.

Public entrypoint: `sync_thread(user_email, thread_id)`. Returns a SyncResult
describing what happened. Idempotent: re-running on the same thread is safe
(messages already in Notion are skipped via dedup, stale cache ids self-heal).

NORMALLY CALLED BY THE QUEUE WORKER, not directly: the Gmail webhook enqueues a
`sync_tasks` row and jobs/queue_worker.py calls this. Direct callers are the CLI
(scripts/sync_one.py) and resync_project.rebuild_thread. Because the worker
retries on failure, this must stay idempotent — never assume it runs once.

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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import drive as drive_client
from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import llm as llm_client
from gb_automations.clients import notion as notion_client
from gb_automations.clients import notion_emails_db
from gb_automations.config import EMAIL_TAGS, EMAILS_PROPS, settings
from gb_automations.db import SessionLocal
from gb_automations.models import (
    CompanyCache,
    ContactCache,
    EmailRow,
    ThreadAttachment,
)
from gb_automations.obs import describe_error, log_api_error
from gb_automations.utils.email_cleaning import (
    body_before_quotes,
    clean_body,
    extract_signature_block,
    has_quoted_history_hint,
)
from gb_automations.utils.email_splitting import (
    ExtractedMessage,
    find_under_split_blocks,
    infer_missing_to_fields,
    synthetic_message_id,
)
from gb_automations.utils.history_extraction import extract_history_blocks
from gb_automations.utils.participants import (
    company_from_domain,
    extract_name,
    find_sender_email,
    is_free_mail_domain,
    is_internal,
    parse_participant,
    strict_email_or_empty,
)
from gb_automations.utils.phone import extract_phone
from gb_automations.utils.signature_parsing import (
    clean_phone_line,
    clean_title_line,
    parse_signature,
)

logger = logging.getLogger(__name__)

# Gmail's per-callout text element limit; we chunk longer bodies.
NOTION_RICH_TEXT_CHUNK = 1900


@dataclass
class SyncResult:
    thread_id: str
    project_name: str | None
    project_page_id: str | None
    thread_subject: str | None = None  # subject of the first message; populated after Gmail fetch
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


async def sync_thread(
    user_email: str,
    thread_id: str,
    on_resolved: Callable[[str, str | None], Awaitable[None]] | None = None,
) -> SyncResult:
    """Sync one Gmail thread into Notion. See module docstring for behavior.

    `on_resolved`, if given, is awaited with `(subject, project_page_id)` as soon
    as both are known (after the Gmail fetch + project match; project_page_id is
    None if the thread matches no project). The queue worker uses this to
    backfill the Notion "Sync Queue" row's subject and to light the Projects-DB
    sync dot mid-flight. Best-effort by contract — callers swallow their own
    errors. Fired once; if the thread matches no project it's still called (with
    the subject and None) so the mirror subject can be backfilled.
    """
    started = time.monotonic()
    result = SyncResult(thread_id=thread_id, project_name=None, project_page_id=None)
    logger.debug("🧵 sync_thread start: thread=%s user=%s", thread_id, user_email)

    # 1. Fetch thread + label map from Gmail (sync API, threadpool-wrapped)
    thread = await asyncio.to_thread(gmail_client.get_thread, user_email, thread_id)
    labels = await asyncio.to_thread(gmail_client.list_labels, user_email)
    label_id_to_name = {label["id"]: label["name"] for label in labels}
    result.messages_seen = len(thread.messages)

    if not thread.messages:
        result.skipped_reason = "thread has no messages"
        logger.info("🧵 sync start: <empty thread> %s for %s", thread_id, user_email)
        return result

    result.thread_subject = thread.messages[0].subject or "(no subject)"
    logger.info(
        "🧵 sync start: %r (%d msg) thread=%s for %s",
        result.thread_subject,
        result.messages_seen,
        thread_id,
        user_email,
    )

    # 2. Pick the Notion projects for this thread. Multiple labels (dual-tagged
    # thread) all flow into the Project relation — Notion supports many-target
    # relations and the team treats this as "this email is shared across
    # projects" rather than "pick one." See _pick_projects docstring.
    project_map = await notion_client.get_project_pages()
    thread_label_names = _collect_thread_label_names(thread.messages, label_id_to_name)
    project_names, project_page_ids, project_label_paths = _pick_projects(
        thread_label_names, project_map
    )
    result.project_name = ", ".join(project_names) or None
    result.project_page_id = project_page_ids[0] if project_page_ids else None
    if on_resolved is not None:
        await on_resolved(result.thread_subject, result.project_page_id)
    if not project_page_ids:
        result.skipped_reason = (
            f"no Notion project matches any thread label "
            f"(thread labels: {sorted(thread_label_names)})"
        )
        return result
    logger.info(
        "  • matched %d project(s): %s",
        len(project_page_ids),
        ", ".join(repr(n) for n in project_names),
    )
    logger.debug(
        "  • project page IDs: %s",
        ", ".join(f"{n!r}={p}" for n, p in zip(project_names, project_page_ids)),
    )

    # 3 + 4 + 5. Use one DB session for the whole thread so cache writes are
    # atomic. The history-reconstruction LLM call needs the session for the
    # first-encounter check, so we open the session before §3.
    async with SessionLocal() as session:
        # 3. Extract pre-thread email history from the first message's quoted
        # content. Regex first; if any extracted block still contains an
        # un-split inner forward (e.g. an Outlook variant the regex layer
        # doesn't recognize) the LLM fallback re-splits that block. Content-
        # hashed synthetic IDs make re-runs idempotent regardless of which
        # layer produced a given message.
        splits = await _extract_history_for_thread(thread)
        try:
            # Serialize concurrent syncs of the same thread. Without this, two
            # Gmail pushes that fire ~seconds apart (e.g. self-emails fire one
            # push for INBOX and one for SENT) race past the EmailRow /
            # Notion-side dedup gates and produce duplicate rows.
            await _acquire_thread_lock(session, thread_id)

            contact_ids, sender_signature_lines = await _upsert_thread_contacts(
                thread, splits, session
            )
            result.contacts_upserted = len(contact_ids)
            logger.info("  • upserted %d contact(s)", result.contacts_upserted)

            # One tracker per thread — replies re-carry attachments from earlier
            # messages, and we only want to upload each unique-byte attachment
            # once (to the original sender's row). Seed it from the durable
            # ThreadAttachment record (sha1 → Drive links) so dedup survives
            # across syncs: a later reply that re-carries a quoted attachment
            # won't re-upload bytes a previous push already sent to Drive, but
            # the stored links still let us set the new row's Files property.
            thread_tracker = ThreadAttachmentTracker()
            prior = await session.execute(
                select(
                    ThreadAttachment.content_sha1, ThreadAttachment.drive_links
                ).where(ThreadAttachment.gmail_thread_id == thread_id)
            )
            for content_sha1, drive_links in prior:
                thread_tracker.links_by_sha1[content_sha1] = drive_links or []
            for msg in thread.messages:
                try:
                    created_count, skipped_count = await _sync_message(
                        msg=msg,
                        extracted=splits.get(msg.message_id, []),
                        project_page_ids=project_page_ids,
                        project_label_paths=project_label_paths,
                        user_email=user_email,
                        session=session,
                        contact_ids=contact_ids,
                        sender_signature_lines=sender_signature_lines,
                        thread_tracker=thread_tracker,
                    )
                    result.rows_created += created_count
                    result.rows_already_present += skipped_count
                except Exception as err:  # one bad message shouldn't kill the thread
                    log_api_error(logger, f"failed to sync message {msg.message_id}", err)
                    result.errors.append(f"{msg.message_id}: {describe_error(err)}")

            await session.commit()
        except Exception:
            await session.rollback()
            raise

    elapsed = time.monotonic() - started
    logger.info(
        "🧵 sync done in %.1fs: +%d row(s), %d already present, %d contact(s) [thread=%s]",
        elapsed,
        result.rows_created,
        result.rows_already_present,
        result.contacts_upserted,
        thread_id,
    )
    return result


# ============================================================
# Pre-thread history extraction (regex, runs every sync)
# ============================================================


async def _extract_history_for_thread(
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
        return {}

    extracted = extract_history_blocks(
        raw_body=first_msg.plain_body,
        parent_subject=first_msg.subject,
        parent_date=first_msg.date,
    )
    if not extracted:
        return {}

    logger.info("  • extracted %d prior email(s) from history (regex)", len(extracted))

    # Two-layer splitter: regex first, LLM only on the blocks that still
    # contain un-split inner headers. Each new mail client formats forwards
    # differently (Outlook bolds labels with `*Fra:*`, mobile clients use
    # idiosyncratic separators, …) — the regex layer handles the common
    # cases; this fallback rescues the long tail without paying LLM cost
    # on every thread.
    extracted = await _resplit_under_split_blocks(
        extracted,
        parent_subject=first_msg.subject,
        parent_date=first_msg.date,
    )

    # Fill missing To from the immediately-prior block's From. Inline reply
    # boundaries ("X skrev Y:") only carry the sender; in a chronological
    # reply chain msg N's recipient is almost always msg N-1's sender.
    infer_missing_to_fields(extracted)

    return {first_msg.message_id: extracted}


async def _resplit_under_split_blocks(
    extracted: list[ExtractedMessage],
    *,
    parent_subject: str,
    parent_date: datetime,
) -> list[ExtractedMessage]:
    """Run the LLM splitter on any block whose `raw_body` still has inner headers.

    Non-flagged blocks pass through unchanged. Flagged blocks are replaced by
    whatever the LLM returns; if the LLM returns [], the original regex block
    is kept (graceful degradation — bad LLM is no worse than today's regex).

    Final result is sorted chronologically so caller ordering invariants hold.
    """
    flagged = find_under_split_blocks(extracted)
    if not flagged:
        return extracted

    logger.info(
        "  • %d block(s) appear under-split; invoking LLM fallback splitter…",
        len(flagged),
    )
    flagged_set = set(flagged)
    rebuilt: list[ExtractedMessage] = []
    for i, block in enumerate(extracted):
        if i not in flagged_set:
            rebuilt.append(block)
            continue
        llm_parts = await llm_client.split_history(
            block.raw_body or block.body,
            parent_subject=parent_subject,
            parent_date=parent_date,
        )
        if not llm_parts:
            # Keep the regex block as-is when the LLM gave us nothing usable.
            rebuilt.append(block)
            continue
        logger.info(
            "    ↳ LLM re-split block %d into %d message(s)", i, len(llm_parts)
        )
        rebuilt.extend(llm_parts)

    rebuilt.sort(key=lambda m: m.date)
    return rebuilt


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


def _pick_projects(
    thread_label_names: set[str], project_map: dict[str, dict[str, str]]
) -> tuple[list[str], list[str], list[str]]:
    """Find EVERY thread-label whose name matches a Notion project's label path.

    `project_map` keys are full nested Gmail label paths (e.g.
    "Projects/2026/Acme"), produced by `notion_client.get_project_pages()`.
    Gmail thread labels carry the same nested name, so set intersection works
    without rebuilding paths here.

    Returns (titles, page_ids, label_paths) — *titles* (leaf names, e.g.
    "Acme") for human-friendly logs/SyncResult, *page_ids* for Notion
    relations, and *label_paths* (full nested names) for downstream Drive
    folder placement. All three lists are aligned in sorted-by-label-path
    order, so the same thread always picks projects in the same order
    regardless of how Gmail orders labels.

    Dual-labeled threads return multiple entries. The `Project` relation on
    each row is set to all of them — Notion relations are multi-target. This
    is intentional: when the team genuinely runs an email across two projects,
    we don't want to silently drop one.
    """
    matched = sorted(thread_label_names & project_map.keys())
    titles = [project_map[name]["title"] for name in matched]
    ids = [project_map[name]["id"] for name in matched]
    return titles, ids, matched


# ============================================================
# Contacts — extract from the whole thread, then upsert each
# ============================================================


# Lower floor for the LLM signature call. Bodies shorter than this never
# contain a usable signature (just a one-liner reply) — skipping saves an
# Ollama round-trip.
_SIG_INPUT_MIN_CHARS = 30
# Upper cap so a pasted-document body doesn't balloon the prompt. Signatures
# sit at the end of a message, so taking the tail is the right slice.
_SIG_INPUT_MAX_CHARS = 2000


def _signature_input(plain_body: str) -> str:
    """Trimmed, capped tail of a message body suitable for the LLM signature call.

    Pipeline: `body_before_quotes` (regex, cuts the quoted reply chain) →
    skip-if-too-short → tail-of-MAX-chars. The trim step prevents us from
    sending a 20-message quoted thread to Ollama just to extract one sender's
    signature; the cap bounds pathological pasted-document bodies.
    """
    trimmed = body_before_quotes(plain_body)
    if len(trimmed) < _SIG_INPUT_MIN_CHARS:
        return ""
    return trimmed[-_SIG_INPUT_MAX_CHARS:]


def _signature_complete(rec: dict[str, Any]) -> bool:
    """True once we've filled the LLM-derived fields (title + phone) for a sender.

    We stop the (expensive) LLM call for a sender as soon as title+phone are both
    set — across their live messages and history segments. Dedup is on SUCCESS,
    not attempt: a signature-only forward that yields nothing must not block
    recovering the real signature from a later (history) segment. Address is NOT
    in this gate: it comes from the cheap regex backstop, which runs on every
    message regardless (some senders legitimately have no address — we'd never
    reach "complete" and would re-call the LLM forever otherwise).
    """
    return all(rec[k] for k in ("title", "phone"))


async def _enrich_sender_from_body(
    sender_record: dict[str, Any],
    *,
    sender_email: str,
    body: str,
    sender_signature_lines: dict[str, str],
) -> None:
    """Fill empty title/phone/address on `sender_record` from one message body.

    LLM locator path first (the model returns the verbatim line containing each
    field; we resolve it against the body and run a deterministic cleaner),
    regex backstop second. Additive — never overwrites a field already set, so
    repeated calls across a sender's messages only fill gaps. Runs on any
    message body: a live `GmailMessage.plain_body` or a history segment's
    `ExtractedMessage.raw_body` — they're treated identically.

    `parse_signature`'s title is intentionally NOT used as a backstop: its
    line-adjacency rule is brittle on real signatures (Rino had a blank line
    between name and title — the LLM handles that trivially).
    """
    # Skip the expensive LLM call once title+phone are in hand, but still fall
    # through to the cheap regex backstop below (it may recover an address we
    # don't have yet, and re-running it is idempotent/additive).
    llm_input = _signature_input(body)
    if llm_input and not _signature_complete(sender_record):
        try:
            loc = await llm_client.classify_signature(
                llm_input, sender_name=sender_record["name"]
            )
        except Exception:
            logger.exception(
                "classify_signature failed for %s; leaving fields empty",
                sender_email,
            )
            loc = llm_client.SignatureLocators(None, None, None)

        known_company = company_from_domain(sender_email)
        llm_title = clean_title_line(
            _resolve_located_line(llm_input, loc.title_line),
            known_company,
            sender_name=sender_record["name"],
        )
        llm_phone = clean_phone_line(_resolve_located_line(llm_input, loc.phone_line))
        if llm_title and not sender_record["title"]:
            sender_record["title"] = llm_title
        if llm_phone and not sender_record["phone"]:
            sender_record["phone"] = llm_phone
        # signature_first_line is used RAW (uncleaned) so the byte-exact
        # slice/partition matching against body lines keeps working.
        if loc.signature_first_line and sender_email not in sender_signature_lines:
            sender_signature_lines[sender_email] = loc.signature_first_line

    # Regex backstops on the signature-shaped slice (sign-off-marker block if
    # present, otherwise the pre-quote body). Phone is a backstop (LLM-primary);
    # address is the SOLE source (a Norwegian address spans two lines — street
    # then postal+city — which `parse_signature` joins, but a single-line LLM
    # locator can't). Title from parse_signature stays dropped — see above.
    sig_source = extract_signature_block(body) or body_before_quotes(body)
    if sig_source:
        if not sender_record["phone"]:
            phone = extract_phone(sig_source)
            if phone:
                sender_record["phone"] = phone
        fields = parse_signature(sig_source, sender_name=sender_record["name"])
        if fields.address and not sender_record["address"]:
            sender_record["address"] = fields.address


async def _row_exists(session: AsyncSession, message_id: str) -> bool:
    """True iff this (real or synthetic) message id is already cached as a row.

    Local-cache only — a hit means we've created the Notion row for this message
    on a prior sync, so its signature enrichment is already persisted and the LLM
    call can be skipped. A miss falls through to the full pipeline (which still
    does its own Notion-side dedup before creating the row), so a stale/cold cache
    only costs the LLM call we'd have made anyway, never a duplicate.
    """
    return await session.get(EmailRow, message_id) is not None


async def _upsert_thread_contacts(
    thread: gmail_client.GmailThread,
    splits: dict[str, list[ExtractedMessage]],
    session: AsyncSession,
) -> tuple[dict[str, str], dict[str, str]]:
    """Walk every external participant in the thread, upsert each to Notion Contacts.

    Returns `(contact_ids, sender_signature_lines)`:
      - `contact_ids`: {email: notion_page_id} for every successfully upserted
        contact.
      - `sender_signature_lines`: {sender_email: signature_first_line} cached
        from the per-sender LLM call. Used downstream by `_slice_at_signature`
        to trim the signature region out of each row's body even when no
        "Mvh"-style sign-off marker is present.

    Also enriches each sender's contact with fields from their signature
    block: phone, title, address, and a relation to a Company row
    (auto-upserted by domain — see _upsert_company). Title, phone, and address
    come from the LLM locator path (it returns the verbatim line containing
    each field; deterministic cleaners turn the line into a value), with the
    regex `parse_signature` / `extract_phone` as offline backstops. Company
    names are NOT parsed from signatures (that heuristic produced garbage rows
    like "Does this mail get added?") — they come from the email-domain stem.

    `splits` is the dict produced by `_presplit_forwarded_chains` — for any
    message that was LLM-split, we also extract participants from each inner
    forwarded message so the original (forwarded) senders/recipients get
    upserted as contacts instead of being lost.
    """
    # email → {name, email, phone, title, address}
    # Domain is always derivable from email, so we don't carry it here; the
    # Company row is titled from the domain stem (see the company upsert below).
    seen: dict[str, dict[str, Any]] = {}
    # email → signature first line (LLM-derived). Used downstream by
    # _slice_at_signature to trim the signature region out of the row body
    # when no regex sign-off marker is present.
    sender_signature_lines: dict[str, str] = {}

    def add_participant(raw: str) -> None:
        # Upsert EVERY participant (internal + external) so the From/To/Cc
        # relations on email rows can resolve to a Contact page regardless of
        # who the participant is. Internal contacts (Goldbox team) are
        # distinguished from external at row-build time via is_internal() on
        # their email — no separate flag needed on the Contact itself.
        parsed = parse_participant(raw)
        if not parsed or not parsed.email:
            return
        if parsed.email not in seen:
            seen[parsed.email] = {
                "name": parsed.name,
                "email": parsed.email,
                "phone": None,
                "title": None,
                "address": None,
            }
        elif parsed.name and not seen[parsed.email]["name"]:
            seen[parsed.email]["name"] = parsed.name

    for msg in thread.messages:
        # 1. Collect every participant from this message's headers.
        for raw in _split_addresses(msg.from_field, msg.to_field, msg.cc_field):
            add_participant(raw)

        # 1b. If this message was a forwarded chain, also walk every participant
        # of each extracted inner message (sender + recipients) so historical
        # participants land in Contacts.
        for inner in splits.get(msg.message_id, []):
            if inner.from_field:
                add_participant(inner.from_field)
            if inner.to_field:
                for raw in _split_addresses(inner.to_field):
                    add_participant(raw)
            if inner.cc_field:
                for raw in _split_addresses(inner.cc_field):
                    add_participant(raw)

        # 2. Enrich the sender's record from their signature. We run the SAME
        # pipeline on the live message and on each forwarded history segment —
        # a split-out history message is just another message by its sender.
        # _enrich_sender_from_body is additive and stops once a sender's
        # title+phone+address are all filled, so a signature-only forward that
        # yields nothing doesn't block recovering the real signature from a
        # later history segment.
        #
        # Skip the (expensive, ~seconds-per-call) classify_signature LLM for any
        # message whose row already exists locally: its enrichment was done on
        # the sync that first created the row and is already persisted in Notion,
        # so re-running it on every reply is pure waste. Participants are still
        # collected above (header walk) so contact relations on the new row stay
        # correct — only the signature LLM is gated.
        sender_email = find_sender_email(msg.from_field)
        if sender_email and sender_email in seen and not await _row_exists(
            session, msg.message_id
        ):
            await _enrich_sender_from_body(
                seen[sender_email],
                sender_email=sender_email,
                body=msg.plain_body,
                sender_signature_lines=sender_signature_lines,
            )

        for inner in splits.get(msg.message_id, []):
            inner_sender = find_sender_email(inner.from_field)
            if not inner_sender or inner_sender not in seen:
                continue
            # raw_body preserves the signature; .body has it stripped by clean_body.
            inner_body = inner.raw_body or inner.body
            if not inner_body:
                continue
            # Same gate, keyed by the synthetic id the history row is stored under.
            synth_id = synthetic_message_id(msg.message_id, inner.from_field, inner.body)
            if await _row_exists(session, synth_id):
                continue
            await _enrich_sender_from_body(
                seen[inner_sender],
                sender_email=inner_sender,
                body=inner_body,
                sender_signature_lines=sender_signature_lines,
            )

    # 3. Upsert Companies (one per unique domain) before contacts, since each
    # contact needs a company_page_id to populate the relation. The company is
    # titled from the email-domain stem (metropolis.no → "Metropolis"); Goldbox
    # can rename the title freely since the dedup key is the domain, not the
    # name (see COMPANIES_PROPS in config.py). We deliberately do NOT title
    # from signature text — that heuristic created garbage rows like
    # "Does this mail get added?" out of ordinary body lines.
    # Free-mail domains (gmail.com, outlook.com, …) represent individuals,
    # not companies — skip them entirely so the Companies DB doesn't get a
    # bogus "Gmail" row, and the contact's Company relation stays empty (the
    # truthful state: we don't know what company this person belongs to).
    domain_to_company_page: dict[str, str] = {}
    for contact in seen.values():
        domain = _domain_of(contact["email"])
        if not domain or domain in domain_to_company_page:
            continue
        if is_free_mail_domain(domain):
            continue
        name = company_from_domain(contact["email"])
        try:
            page_id = await _upsert_company(domain=domain, name=name, session=session)
            if page_id:
                domain_to_company_page[domain] = page_id
        except Exception:
            logger.exception("Failed to upsert company for domain %s", domain)

    # 4. Upsert each contact via cache → Notion.
    out: dict[str, str] = {}
    for contact in seen.values():
        try:
            company_page_id = domain_to_company_page.get(_domain_of(contact["email"]))
            page_id = await _upsert_contact(contact, company_page_id, session)
            if page_id:
                out[contact["email"]] = page_id
        except Exception:
            logger.exception("Failed to upsert contact %s", contact["email"])
    return out, sender_signature_lines


def _split_addresses(*fields: str) -> list[str]:
    """Concatenate from/to/cc header values and split on commas."""
    return [chunk for field in fields if field for chunk in field.split(",")]


def _domain_of(email: str) -> str:
    """Lowercased part after '@', or '' if the email is malformed."""
    return email.split("@", 1)[1].lower() if "@" in email else ""


async def _upsert_contact(
    contact: dict[str, Any],
    company_page_id: str | None,
    session: AsyncSession,
) -> str | None:
    """Find-or-create a contact. Cache first, then Notion email-match, then
    Notion exact-name match, then create.

    Additive-only on any existing row: every field (Email, Phone, Title,
    Address, Company) is only written if currently empty in Notion. We never
    overwrite a value Goldbox put there, on any field including Name and
    Email. See `patch_contact_enrichment`.

    The name fallback exists so a manually-created contact row (a Name but
    no Email yet) gets matched and has its Email filled additively, rather
    than producing a duplicate row. Name match is exact-string and one-result
    only — ambiguous matches fall through to create.

    A name match is only honored when the existing row's Email is empty or
    already equals the incoming address. If the row already has a *different*
    email, the incoming address gets its OWN new row instead of being dropped
    (the single-valued Notion Email field can't hold both, and silently
    discarding the new address — the prior behavior — lost real contacts).
    """
    email = contact["email"]
    name = contact["name"] or contact["email"]
    phone = contact["phone"]
    title = contact["title"]
    address = contact["address"]

    # 1. Local cache hit? Re-query Notion by email so a stale cached id (the
    # contact row was deleted/archived on another host) self-heals: we patch the
    # LIVE row the query returns, not the cached id, and re-cache it. If the
    # query finds nothing, the cache is stale → evict and fall through to create.
    cached = await session.get(ContactCache, email)
    if cached:
        try:
            existing = await notion_client.find_contact_by_email(email)
        except Exception:
            existing = None
        if existing:
            live_id = existing["id"]
            if live_id != cached.notion_page_id:
                logger.info("    cache was stale for contact %s — re-resolved", email)
                await _cache_contact(session, email=email, page_id=live_id)
            try:
                await notion_client.patch_contact_enrichment(
                    live_id,
                    existing_props=existing.get("properties", {}),
                    phone=phone,
                    title=title,
                    address=address,
                    company_page_id=company_page_id,
                )
            except Exception as err:
                log_api_error(logger, f"enrichment failed for {email}", err)
            return live_id
        # Cached but not found live → stale. Evict and fall through to create.
        logger.info("    cache was stale for contact %s (gone) — re-creating", email)
        await session.delete(cached)
        await session.flush()

    # 2. Notion already has this contact by email?
    existing = await notion_client.find_contact_by_email(email)
    if existing:
        await _cache_contact(session, email=email, page_id=existing["id"])
        try:
            await notion_client.patch_contact_enrichment(
                existing["id"],
                existing_props=existing.get("properties", {}),
                phone=phone,
                title=title,
                address=address,
                company_page_id=company_page_id,
            )
        except Exception:
            logger.exception("Enrichment failed for %s", email)
        return existing["id"]

    # 3. Fall back to exact-name match. Catches the "manually-created contact
    # row with a Name but no Email" case. Ambiguous names (multiple matches)
    # intentionally fall through to create — guessing is worse than duplicating.
    # We do NOT use the contact's name from the email if it's just the email
    # address (the `name or email` fallback above), since matching by email-as-
    # name against a real human name would mis-attach.
    if contact["name"]:
        existing_by_name = await notion_client.find_contact_by_name_exact(contact["name"])
        # Only reuse the row when its Email is empty (the manual-row case) or
        # already matches ours. A different existing email means this is a
        # second address for the same name — create a new row rather than drop
        # the incoming address against the single-valued Email field.
        existing_email = (
            notion_client.contact_email_value(existing_by_name.get("properties"))
            if existing_by_name
            else None
        )
        if existing_by_name and (existing_email is None or existing_email == email):
            page_id = existing_by_name["id"]
            await _cache_contact(session, email=email, page_id=page_id)
            try:
                # Email is passed here so the empty Email field on their
                # manually-created row gets filled additively.
                await notion_client.patch_contact_enrichment(
                    page_id,
                    existing_props=existing_by_name.get("properties", {}),
                    email=email,
                    phone=phone,
                    title=title,
                    address=address,
                    company_page_id=company_page_id,
                )
            except Exception:
                logger.exception("Enrichment failed for %s (matched by name)", email)
            logger.info(
                "Matched existing contact %r by name; linked email %s", contact["name"], email
            )
            return page_id

    # 4. Create in Notion.
    created = await notion_client.create_contact(
        name=name,
        email=email,
        phone=phone,
        title=title,
        address=address,
        company_page_id=company_page_id,
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


async def _upsert_company(
    *, domain: str, name: str, session: AsyncSession
) -> str | None:
    """Find-or-create a company row by domain. Cache first, then Notion.

    On a cache hit we re-query Notion by domain so a stale cached id (company
    row deleted/archived on another host) self-heals — otherwise we'd hand a
    dead relation id to create_contact and Notion would reject it. Found-live →
    re-cache + use; not-found → evict and fall through to create.
    """
    cached = await session.get(CompanyCache, domain)
    if cached:
        try:
            existing = await notion_client.find_company_by_domain(domain)
        except Exception:
            # Transient lookup error — trust the cache rather than risk a dup.
            return cached.notion_page_id
        if existing:
            if existing["id"] != cached.notion_page_id:
                logger.info("    cache was stale for company %s — re-resolved", domain)
                await _cache_company(session, domain=domain, page_id=existing["id"])
            return existing["id"]
        logger.info("    cache was stale for company %s (gone) — re-creating", domain)
        await session.delete(cached)
        await session.flush()

    existing = await notion_client.find_company_by_domain(domain)
    if existing:
        await _cache_company(session, domain=domain, page_id=existing["id"])
        return existing["id"]

    created = await notion_client.create_company(name=name, domain=domain)
    page_id = created["id"]
    await _cache_company(session, domain=domain, page_id=page_id)
    logger.info("Created company %s (%s)", name, domain)
    return page_id


async def _cache_company(session: AsyncSession, *, domain: str, page_id: str) -> None:
    stmt = (
        insert(CompanyCache)
        .values(domain=domain, notion_page_id=page_id)
        .on_conflict_do_update(index_elements=["domain"], set_={"notion_page_id": page_id})
    )
    await session.execute(stmt)


# ============================================================
# Per-message sync
# ============================================================


async def _resolve_db_for_date(date: datetime) -> tuple[str, set[str]]:
    """Year-route helper: (db_id, property_names) for a message's date.

    Centralizes the two API calls every sync path needs:
      1. `get_emails_db_for_year(year)` — resolves (and creates if missing)
         the year-partitioned Emails database.
      2. `get_emails_db_property_names(db_id)` — fetches the schema once per
         DB so property-builders only set fields that exist (lets the same
         code work against workspaces with slightly different schemas).
    Both layers cache internally so per-message overhead is just a dict hit
    after the first call of the day.
    """
    db_id = await notion_emails_db.get_emails_db_for_year(date.year)
    props = await notion_client.get_emails_db_property_names(db_id)
    return db_id, props


async def _sync_message(
    *,
    msg: gmail_client.GmailMessage,
    extracted: list[ExtractedMessage],
    project_page_ids: list[str],
    project_label_paths: list[str],
    user_email: str,
    session: AsyncSession,
    contact_ids: dict[str, str],
    sender_signature_lines: dict[str, str] | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Create Notion rows for one Gmail message.

    Always creates one regex-cleaned row for the Gmail message itself (the
    canonical 1:1 mapping). When `extracted` is non-empty (history was
    reconstructed for this message — only happens for the first message of a
    thread on first encounter), ALSO creates one row per extracted prior email.

    Each row routes to its own year DB (resolved from the row's own date —
    matters for threads spanning years and for extracted history blocks dated
    from earlier years).

    Returns `(rows_created, rows_already_present)`.
    """
    # Resolve the LLM-cached signature first line for this sender, if any.
    # Used by _slice_at_signature to trim the signature out of the row body
    # even when the regex sign-off-marker check finds nothing.
    sender_hint: str | None = None
    if sender_signature_lines:
        sender_addr = _addr_to_email(msg.from_field).lower()
        sender_hint = sender_signature_lines.get(sender_addr)

    # Attribute the top-level message's attachments to whichever email
    # actually sent them. For non-forwarded messages this is a no-op (all
    # decisions land on the forwarder bucket = msg's own row).
    forwarder_decisions, by_synth = _attribute_attachments(
        msg, extracted, signature_first_line_hint=sender_hint
    )

    # Standalone callers (e.g. `sync_message`) don't pre-build a tracker;
    # create a per-call one. Real thread syncs always pass theirs in.
    if thread_tracker is None:
        thread_tracker = ThreadAttachmentTracker()

    # 1. The regex single-row path runs for every Gmail message.
    created, skipped = await _sync_single_message(
        msg=msg,
        project_page_ids=project_page_ids,
        project_label_paths=project_label_paths,
        user_email=user_email,
        session=session,
        contact_ids=contact_ids,
        attachment_decisions=forwarder_decisions,
        signature_first_line_hint=sender_hint,
        thread_tracker=thread_tracker,
    )

    # 2. If history was reconstructed for this message, create rows for the
    # prior emails too. The LLM prompt is configured to extract ONLY the
    # quoted history (not the top-level message), so there's no overlap with
    # the regex row from §1.
    if extracted:
        logger.info(
            "  → %r: reconstructing %d prior email(s)",
            msg.subject or "(no subject)",
            len(extracted),
        )
        logger.debug("       (message_id=%s)", msg.message_id)
        c, s = await _sync_forwarded_chain(
            parent_msg=msg,
            extracted=extracted,
            project_page_ids=project_page_ids,
            project_label_paths=project_label_paths,
            user_email=user_email,
            session=session,
            contact_ids=contact_ids,
            attachments_by_synth=by_synth,
            sender_signature_lines=sender_signature_lines,
            thread_tracker=thread_tracker,
        )
        created += c
        skipped += s

    return (created, skipped)


async def _sync_single_message(
    *,
    msg: gmail_client.GmailMessage,
    project_page_ids: list[str],
    project_label_paths: list[str] | None = None,
    user_email: str,
    session: AsyncSession,
    contact_ids: dict[str, str] | None = None,
    attachment_decisions: list[AttachmentDecision] | None = None,
    signature_first_line_hint: str | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Sync a non-forwarded Gmail message into one Notion row.

    `attachment_decisions` is the subset of Gmail attachments attributed to
    THIS row (the forwarder's bucket from `_attribute_attachments`). When
    None, falls back to partitioning the message's own attachments — used by
    the standalone `sync_message` path that doesn't go through history
    extraction.

    The target Emails DB is resolved from `msg.date.year` — year-partitioned
    DBs each carry the same schema, populated on demand.

    Returns `(1, 0)` if a new row was created, `(0, 1)` if it was already there.
    """
    # Attachments attributed to THIS message's row, used by both the create
    # path and the dedup-hit re-link below. For a non-forwarded message `msg`
    # itself carries the bytes, so it's also the parent_msg.
    if attachment_decisions is None:
        attachment_decisions = _partition_attachments(msg.attachments)
    attributed_sender = _addr_to_email(msg.from_field) or user_email
    tracker = thread_tracker or ThreadAttachmentTracker()

    # 1. Local cache hit? Re-PATCH the Project relation in case the user has
    # swapped labels since the row was created (mis-labeled → corrected). The
    # PATCH is idempotent so we can do this unconditionally; the Notion-side
    # feedback-loop filter swallows the resulting webhook echo. Also re-link the
    # Files property — a row created in a sync where the upload was deduped (or
    # by an older build) never got its files; this self-heals it.
    cached = await session.get(EmailRow, msg.message_id)
    if cached and not await _evict_if_stale_email_row(session, cached, msg.message_id):
        logger.debug("    dedup hit (local cache) for msg %s", msg.message_id)
        try:
            await notion_client.patch_email_row_project(
                cached.notion_page_id, project_page_ids
            )
        except Exception as err:
            log_api_error(
                logger, f"    project reconciliation failed for msg {msg.message_id}", err
            )
        _db_id, emails_db_props = await _resolve_db_for_date(msg.date)
        await _relink_existing_row_files(
            parent_msg=msg,
            decisions=attachment_decisions,
            attributed_sender=attributed_sender,
            row_id=cached.notion_page_id,
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
        return (0, 1)
    # cache miss, or the cached id was stale and just evicted → re-resolve below.

    db_id, emails_db_props = await _resolve_db_for_date(msg.date)

    # 2. Notion already has a row for this message ID (different user synced it)?
    # Same reconciliation rationale as the local-cache branch.
    existing = await notion_client.find_email_row_by_message_id(msg.message_id, db_id)
    if existing:
        logger.debug("    dedup hit (Notion query) for msg %s", msg.message_id)
        try:
            await notion_client.patch_email_row_project(
                existing["id"], project_page_ids
            )
        except Exception as err:
            log_api_error(
                logger, f"    project reconciliation failed for msg {msg.message_id}", err
            )
        await _relink_existing_row_files(
            parent_msg=msg,
            decisions=attachment_decisions,
            attributed_sender=attributed_sender,
            row_id=existing["id"],
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
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
    has_potential_attachments = any(d.upload for d in attachment_decisions)
    # Drop the sender's signature from the row body. The LLM gave us the
    # verbatim first line of the signature region; cut there. Contacts row
    # already carries the structured fields (title, phone, address, company).
    # No-op when the hint is missing or doesn't match — same fallback the
    # attachment partitioner uses.
    body_for_row = _slice_at_signature(msg.plain_body, signature_first_line_hint)
    # Keep `[image: NAME]` markers in the body for attachments we're actually
    # uploading — preserves the in-body anchor so Goldbox can see "the image
    # the sender referenced sits HERE in the paragraph." Skipped images
    # (tiny signature/tracking thumbnails) have their markers stripped so the
    # body doesn't reference files that aren't in the row.
    cleaned_body = clean_body(
        body_for_row,
        keep_image_markers=_owning_filenames(attachment_decisions),
    )
    if not cleaned_body and not has_potential_attachments:
        logger.info(
            "    ⊘ skipping msg %r: no body content and no real attachments "
            "(signature-only forward; see extracted history)",
            msg.subject or "(no subject)",
        )
        logger.debug("       (message_id=%s)", msg.message_id)
        return (0, 0)

    # 4. Create the row.
    logger.info(
        "    📝 row (gmail message): %s",
        _format_extraction_preview(msg.from_field, msg.subject, cleaned_body),
    )
    logger.debug("       (message_id=%s)", msg.message_id)
    properties = await _build_email_row_properties(
        msg=msg,
        project_page_ids=project_page_ids,
        user_email=user_email,
        emails_db_props=emails_db_props,
        body=cleaned_body,
        contact_ids=contact_ids or {},
    )
    created = await notion_client.create_email_row(properties, db_id)
    row_id = created["id"]

    # Upload attachments to Drive and set the row's Files property. Per-
    # attachment errors are logged and don't block the rest. Row already
    # exists at this point, so a total upload failure still leaves a valid
    # (file-less) row in Notion.
    if has_potential_attachments and EMAILS_PROPS.get("files") in emails_db_props:
        uploaded = await _upload_attachments(
            parent_msg=msg,
            decisions=attachment_decisions,
            attributed_sender=attributed_sender,
            user_email=user_email,
            session=session,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
        if uploaded:
            try:
                await notion_client.patch_email_row_files(row_id, uploaded)
            except Exception:
                logger.exception(
                    "Failed to set Files property on %s after upload", row_id
                )

    # Append the chat-style callout for this message to the row's page body.
    blocks = _build_chat_blocks(msg, user_email, body=cleaned_body)
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
    project_page_ids: list[str],
    project_label_paths: list[str],
    user_email: str,
    session: AsyncSession,
    contact_ids: dict[str, str],
    attachments_by_synth: dict[str, list[AttachmentDecision]] | None = None,
    sender_signature_lines: dict[str, str] | None = None,
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
    sender_signature_lines = sender_signature_lines or {}

    for inner in extracted:
        synth_id = synthetic_message_id(
            parent_msg.message_id, inner.from_field, inner.body
        )
        # This segment's sender may differ from the forwarder — resolve their
        # own signature first line so the signature is sliced out of the body.
        inner_sender = _addr_to_email(inner.from_field).lower()
        inner_hint = sender_signature_lines.get(inner_sender)
        c, s = await _sync_extracted_message(
            parent_msg=parent_msg,
            inner=inner,
            synthetic_id=synth_id,
            project_page_ids=project_page_ids,
            project_label_paths=project_label_paths,
            user_email=user_email,
            session=session,
            contact_ids=contact_ids,
            attachment_decisions=attachments_by_synth.get(synth_id, []),
            signature_first_line_hint=inner_hint,
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
    project_page_ids: list[str],
    project_label_paths: list[str],
    user_email: str,
    session: AsyncSession,
    contact_ids: dict[str, str],
    attachment_decisions: list[AttachmentDecision] | None = None,
    signature_first_line_hint: str | None = None,
    thread_tracker: ThreadAttachmentTracker | None = None,
) -> tuple[int, int]:
    """Create a Notion row for one LLM-extracted sub-message.

    `attachment_decisions` are the Gmail attachments attributed to THIS
    historical email (by filename mention in `inner.body`). Bytes still come
    from `parent_msg` — that's the message that physically carries them.

    The row lands in the year DB matching `inner.date.year` — so a 2023
    email forwarded in 2026 still ends up in `Emails 2023`, preserving
    chronological partitioning even when discovery is years later.

    Returns `(1, 0)` if created, `(0, 1)` if already present (dedup by synthetic_id).
    """
    decisions = attachment_decisions or []
    inner_sender = _addr_to_email(inner.from_field) or _addr_to_email(
        parent_msg.from_field
    ) or user_email
    tracker = thread_tracker or ThreadAttachmentTracker()

    cached = await session.get(EmailRow, synthetic_id)
    if cached and not await _evict_if_stale_email_row(session, cached, synthetic_id):
        logger.debug("    dedup hit (local cache) for extracted %s", synthetic_id)
        try:
            await notion_client.patch_email_row_project(
                cached.notion_page_id, project_page_ids
            )
        except Exception as err:
            log_api_error(
                logger, f"    project reconciliation failed for extracted {synthetic_id}", err
            )
        _db_id, emails_db_props = await _resolve_db_for_date(inner.date)
        await _relink_existing_row_files(
            parent_msg=parent_msg,
            decisions=decisions,
            attributed_sender=inner_sender,
            row_id=cached.notion_page_id,
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
        return (0, 1)
    # cache miss / stale-evicted → re-resolve below.

    db_id, emails_db_props = await _resolve_db_for_date(inner.date)

    existing = await notion_client.find_email_row_by_message_id(synthetic_id, db_id)
    if existing:
        logger.debug("    dedup hit (Notion query) for extracted %s", synthetic_id)
        try:
            await notion_client.patch_email_row_project(
                existing["id"], project_page_ids
            )
        except Exception as err:
            log_api_error(
                logger, f"    project reconciliation failed for extracted {synthetic_id}", err
            )
        await _relink_existing_row_files(
            parent_msg=parent_msg,
            decisions=decisions,
            attributed_sender=inner_sender,
            row_id=existing["id"],
            user_email=user_email,
            session=session,
            emails_db_props=emails_db_props,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
        await _cache_email_row(
            session,
            message_id=synthetic_id,
            thread_id=parent_msg.thread_id,
            notion_page_id=existing["id"],
            seen_by_email=user_email,
        )
        return (0, 1)

    # Compute the row's display body once. Re-clean from raw_body so we
    # control which `[image: NAME]` markers survive (the ones for attachments
    # actually attributed to this row). Fall back to inner.body for callers
    # that didn't populate raw_body (older tests, future producers).
    keep_filenames = _owning_filenames(decisions)
    if inner.raw_body:
        display_body = clean_body(inner.raw_body, keep_image_markers=keep_filenames)
    else:
        display_body = inner.body
    # Slice off the sender's signature. clean_body only cuts at a sign-off
    # MARKER ("Mvh", …); a marker-less signature (e.g. Rino's) survives, so we
    # also cut at the LLM-located signature first line — the same hint the live
    # path uses. Safe no-op when the hint is absent or doesn't match.
    display_body = _slice_at_signature(display_body, signature_first_line_hint)

    logger.info(
        "    📝 row (history): %s",
        _format_extraction_preview(inner.from_field, inner.subject, display_body),
    )
    logger.debug("       (synthetic_id=%s)", synthetic_id)
    properties = await _build_extracted_row_properties(
        parent_msg=parent_msg,
        inner=inner,
        synthetic_id=synthetic_id,
        project_page_ids=project_page_ids,
        user_email=user_email,
        emails_db_props=emails_db_props,
        body=display_body,
        contact_ids=contact_ids,
    )
    notion_page = await notion_client.create_email_row(properties, db_id)
    row_id = notion_page["id"]

    # Upload any attachments attributed to THIS historical email. Bytes live
    # on parent_msg (the forwarder's Gmail message); dedup runs against
    # inner.from_field so the original sender, not the forwarder, owns them.
    has_uploadable = any(d.upload for d in decisions)
    if has_uploadable and EMAILS_PROPS.get("files") in emails_db_props:
        uploaded = await _upload_attachments(
            parent_msg=parent_msg,
            decisions=decisions,
            attributed_sender=inner_sender,
            user_email=user_email,
            session=session,
            thread_tracker=tracker,
            project_label_paths=project_label_paths or [],
        )
        if uploaded:
            try:
                await notion_client.patch_email_row_files(row_id, uploaded)
            except Exception:
                logger.exception(
                    "Failed to set Files property on extracted row %s", row_id
                )

    blocks = _build_extracted_chat_blocks(inner, user_email, body=display_body)
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


async def _evict_if_stale_email_row(
    session: AsyncSession, cached: EmailRow, message_id: str
) -> bool:
    """If the cached row's Notion page is gone/archived, evict it. Returns evicted?

    A per-machine cache can hold a `notion_page_id` whose page (or whose parent
    Emails DB) was deleted on another host — patching it then fails with a 404
    or "archived ancestor" 400. We validate once on the cache-hit path; if stale,
    delete the local row so the caller falls through to the Notion re-query/
    create path (which re-links a live row or makes a fresh one). Self-healing,
    counts as a clean sync. A transient (non-stale) error re-raises, so we never
    discard a good cache entry over a network blip.
    """
    try:
        live = await notion_client.page_is_live(cached.notion_page_id)
    except Exception:
        # Non-stale error (network/auth) — trust the cache, let the normal path
        # try and surface any real failure there.
        return False
    if live:
        return False
    logger.info(
        "    cache was stale for msg %s (page %s gone/archived) — re-resolving",
        message_id,
        cached.notion_page_id,
    )
    await session.delete(cached)
    await session.flush()
    return True


# ============================================================
# Notion property + block builders
# ============================================================


def _emails_from(raw_field: str) -> list[str]:
    """Parse a raw `To`/`Cc` header value into a list of lowercased emails.

    Empty strings, names without emails, and duplicates are dropped. Order
    is preserved (Notion renders relations in insertion order).
    """
    if not raw_field:
        return []
    seen: list[str] = []
    for raw in _split_addresses(raw_field):
        parsed = parse_participant(raw)
        if not parsed or not parsed.email:
            continue
        if parsed.email not in seen:
            seen.append(parsed.email)
    return seen


async def _build_email_row_properties(
    *,
    msg: gmail_client.GmailMessage,
    project_page_ids: list[str],
    user_email: str,
    emails_db_props: set[str],
    body: str,
    contact_ids: dict[str, str],
) -> dict[str, Any]:
    """Build the Notion properties dict for a single-message Emails-DB row.

    `body` is the already-cleaned text the row should display — callers are
    responsible for running `clean_body` with the right `keep_image_markers`
    set, since which attachments end up on this row determines which markers
    should be preserved.

    `contact_ids` maps every participant email seen during this thread to
    the Notion Contact page ID, populated upstream by `_upsert_thread_contacts`.
    Used to resolve the From/To/Cc relation properties.

    Only sets properties that exist in `emails_db_props` (the actual DB schema),
    so the same code works against workspaces with different schemas.
    """
    from_email = _addr_to_email(msg.from_field)
    to_emails = _emails_from(msg.to_field)
    cc_emails = _emails_from(msg.cc_field)

    return await _assemble_row_props(
        emails_db_props=emails_db_props,
        subject=msg.subject,
        thread_id=msg.thread_id,
        message_id=msg.message_id,
        project_page_ids=project_page_ids,
        from_email=from_email,
        to_emails=to_emails,
        cc_emails=cc_emails,
        contact_ids=contact_ids,
        date_iso=msg.date.isoformat(),
        body=body,
    )


async def _build_extracted_row_properties(
    *,
    parent_msg: gmail_client.GmailMessage,
    inner: ExtractedMessage,
    synthetic_id: str,
    project_page_ids: list[str],
    user_email: str,
    emails_db_props: set[str],
    body: str,
    contact_ids: dict[str, str],
) -> dict[str, Any]:
    """Build Notion properties for an LLM-extracted sub-message.

    `body` is the display body the row should carry — caller computes it
    from `inner.raw_body` with the keep-list set to whichever attachment
    filenames this row owns, so in-body `[image: NAME]` anchors survive
    only for attachments the row actually carries.

    `contact_ids` is the same shared map used by the regular path. Extracted
    messages may have empty `to_field`/`cc_field` (inline-reply boundaries
    don't carry recipient info) — in that case To/Cc relations are simply
    omitted from this row.

    Same property shape as a regular row — synthetic_id goes into the
    message_id property so dedup queries work unchanged.
    """
    # LLM-extracted messages may return just a display name ("Petter Burhol")
    # with no email address. Use strict parsing here — better to leave the
    # relation unset than to invent one.
    from_email = strict_email_or_empty(inner.from_field)
    to_emails = _emails_from(inner.to_field)
    cc_emails = _emails_from(inner.cc_field)

    return await _assemble_row_props(
        emails_db_props=emails_db_props,
        subject=inner.subject,
        thread_id=parent_msg.thread_id,
        message_id=synthetic_id,
        project_page_ids=project_page_ids,
        from_email=from_email,
        to_emails=to_emails,
        cc_emails=cc_emails,
        contact_ids=contact_ids,
        date_iso=inner.date.isoformat(),
        body=body,
    )


async def _assemble_row_props(
    *,
    emails_db_props: set[str],
    subject: str,
    thread_id: str,
    message_id: str,
    project_page_ids: list[str],
    from_email: str,
    to_emails: list[str],
    cc_emails: list[str],
    contact_ids: dict[str, str],
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
        else:
            # The LLM ran (timing logged in clients/llm.py) but chose no tag
            # from the taxonomy. Worth surfacing so it doesn't look like
            # tagging was silently skipped or quietly failed.
            logger.info("    🏷  no tag matched")

    from_contact_id = contact_ids.get(from_email) if from_email else None
    to_contact_ids = [contact_ids[e] for e in to_emails if e in contact_ids]
    cc_contact_ids = [contact_ids[e] for e in cc_emails if e in contact_ids]

    props: dict[str, Any] = {}

    def maybe_set(key: str, value: dict[str, Any]) -> None:
        name = EMAILS_PROPS[key]
        if name in emails_db_props:
            props[name] = value

    maybe_set("subject", {"title": [{"text": {"content": (subject or "(no subject)")[:1900]}}]})
    maybe_set("thread_id", {"rich_text": [{"text": {"content": thread_id}}]})
    maybe_set("message_id", {"rich_text": [{"text": {"content": message_id}}]})
    maybe_set("project", {"relation": [{"id": pid} for pid in project_page_ids]})
    if from_contact_id:
        maybe_set("from_contact", {"relation": [{"id": from_contact_id}]})
    if to_contact_ids:
        maybe_set("to_contacts", {"relation": [{"id": cid} for cid in to_contact_ids]})
    if cc_contact_ids:
        maybe_set("cc_contacts", {"relation": [{"id": cid} for cid in cc_contact_ids]})
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


def _build_chat_blocks(
    msg: gmail_client.GmailMessage,
    user_email: str,
    *,
    body: str | None = None,
) -> list[dict[str, Any]]:
    """Render one Gmail message as a chat-style block group for a Notion page body.

    `body` overrides the default cleaned body so the page-content text stays
    in sync with the row-property text when the caller used a keep-list to
    preserve image markers for owned attachments.
    """
    from_email = _addr_to_email(msg.from_field)
    from_name = extract_name(msg.from_field) or from_email
    is_outgoing = from_email == user_email.lower()

    if body is None:
        body = clean_body(msg.plain_body)
    body = body or "_(no message body — forwarded or empty)_"
    timestamp = msg.date.strftime("%b %d, %Y · %H:%M UTC")
    return _assemble_chat_blocks(
        from_name=from_name,
        body=body,
        timestamp=timestamp,
        is_outgoing=is_outgoing,
    )


def _build_extracted_chat_blocks(
    inner: ExtractedMessage, user_email: str, *, body: str | None = None
) -> list[dict[str, Any]]:
    """Render an LLM-extracted sub-message as a chat-style block group.

    `body` overrides `inner.body` for the rendered text — used so the
    page-content body and the row-property body stay in sync when the
    caller computed a custom display body (e.g. with `[image: NAME]`
    markers preserved for owned attachments).
    """
    from_email = strict_email_or_empty(inner.from_field)
    from_name = extract_name(inner.from_field) or from_email or "(unknown)"
    is_outgoing = bool(from_email) and from_email == user_email.lower()
    body = (body if body is not None else inner.body) or "_(no message body)_"
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
    """Verdict for one Gmail attachment after byte-free filtering.

    Used so the upload loop can log which attachments are being skipped and
    why, without losing the original attachment metadata. The size/inline-
    repeat checks run synchronously (no bytes needed); the cross-message
    repetition check (which DOES need bytes) happens later in the upload loop.
    """

    attachment: gmail_client.GmailAttachment
    upload: bool
    skip_reason: str = ""


def _owning_filenames(decisions: list[AttachmentDecision]) -> set[str]:
    """Filenames whose `[image: NAME]` markers should be kept in this row's body.

    Only attachments that will actually be uploaded (`upload=True`) qualify —
    keeping a marker for a skipped signature image would leave a dangling
    reference to a file that isn't in the row. Tiny-image (sub-1KB) decisions
    have `upload=False` and so don't appear in the keep set.
    """
    return {d.attachment.filename for d in decisions if d.upload and d.attachment.filename}


def _body_mentions_filename(body: str, filename: str) -> bool:
    """True if `filename` appears in `body` — as `[image: name]` OR a bare mention.

    Used to attribute attachments on a forwarded Gmail message to the extracted
    historical email that originally sent them. Matches case-insensitively and
    treats the filename as a literal substring (no globbing).
    """
    if not body or not filename:
        return False
    return filename.lower() in body.lower()


_TINY_IMAGE_BYTES = 1024  # 1 KB — Word's `~WRDxxxx.jpg` thumbnails and other
# Office-generated signature artifacts always fall below this. Real photos,
# logos sent as project assets, and even small JPEGs sit comfortably above
# 1 KB. Keeps the false-positive risk on legitimate small client logos low.


def _find_line_index(body: str, needle: str) -> int:
    """Return index of the first line in `body` that equals `needle` (whitespace-stripped).

    Strict line equality on stripped content — not substring — so a short
    needle like "Hei" doesn't false-match a body line "Hei Petter,". -1 if
    not found. Used to resolve an LLM-supplied signature_first_line into a
    line index within the current message's body.
    """
    if not body or not needle:
        return -1
    target = needle.strip()
    if not target:
        return -1
    for i, line in enumerate(body.split("\n")):
        if line.strip() == target:
            return i
    return -1


def _resolve_located_line(body: str, line: str | None) -> str | None:
    """Resolve an LLM-located line to the actual body line it points at.

    The LLM is asked for verbatim lines, but may return one that's slightly off
    (a dropped char, a smart quote). We prefer the real body text:
      1. strict line-equality match → that body line;
      2. case-insensitive substring overlap → that body line;
      3. last resort → the LLM's own string (the cleaners are robust enough to
         still extract a value, which beats dropping the field).
    """
    if not line:
        return None
    idx = _find_line_index(body, line)
    if idx >= 0:
        return body.split("\n")[idx].strip()
    needle = line.strip().lower()
    if needle:
        for body_line in body.split("\n"):
            stripped = body_line.strip()
            low = stripped.lower()
            if low and (needle in low or low in needle):
                return stripped
    return line.strip()


def _slice_at_signature(body: str, signature_first_line: str | None) -> str:
    """Return `body` truncated to everything BEFORE the signature's first line.

    The signature itself (name, title, phone, email, address, company,
    inline-image markers below it) is already captured on the Contacts row,
    so leaving it in the row body is just visual noise. When the hint doesn't
    match (or no hint was provided), `body` comes back unchanged — same safe
    no-op pattern as elsewhere.

    Two-tier match on the hint (usually the sender's name, e.g. "Rino Larsen"):
      1. A body LINE that equals the hint → cut from that line.
      2. Otherwise, the first line that CONTAINS the hint → cut from there,
         keeping any text before it on that line. Covers the marker-less
         signature whose lines `_strip_html` left mushed onto one line
         ("Supert :)Rino LarsenDaglig leder…"): we still trim from the name on.
    """
    if not signature_first_line:
        return body
    needle = signature_first_line.strip()
    if not needle:
        return body
    lines = body.split("\n")
    # Tier 1: exact line match.
    idx = _find_line_index(body, signature_first_line)
    if idx >= 0:
        return "\n".join(lines[:idx])
    # Tier 2: the hint is embedded inside a line — cut at its position, keeping
    # the preceding text (and any fully-preceding lines).
    for i, line in enumerate(lines):
        pos = line.find(needle)
        if pos >= 0:
            head_lines = lines[:i]
            prefix = line[:pos].rstrip()
            if prefix:
                head_lines.append(prefix)
            return "\n".join(head_lines)
    return body


def _partition_attachments(
    attachments: list[gmail_client.GmailAttachment],
) -> list[AttachmentDecision]:
    """Decide which attachments are worth downloading + uploading.

    Only ONE skip rule survives — and it's the single most false-positive-safe
    one we have:
      - Tiny-image: image/* attachments under 1 KB are Office-generated
        signature thumbnails / tracking pixels (e.g. `~WRD0002.jpg`, a 1×1 gif),
        never real content. No real photo, logo, or document is under 1 KB, so
        this drops zero real files while keeping the Notion rows free of the
        tracking-pixel flood every Outlook email carries.

    Every other heuristic was REMOVED on purpose. The earlier rules
    (inline-repeated, fuzzy ±size same-name, repeating-signature) were guesses
    that could drop a *real* distinct file — a revised CAD/PDF re-sent under the
    same name, a recurring legitimate attachment from a frequent sender. The
    operating rule now is the client's: if a file is sent, it must appear in
    Notion. Byte-identical re-carries (Gmail quoting an attachment on every
    reply) are still de-duplicated for *upload* by content sha1 in the upload
    loop (`ThreadAttachmentTracker`) — but that dedup re-links the row instead
    of dropping the file, so nothing real is ever lost.

    Returns one `AttachmentDecision` per input attachment, preserving order.
    """
    if not attachments:
        return []
    out: list[AttachmentDecision] = []
    for att in attachments:
        if (
            att.mime_type.startswith("image/")
            and att.size > 0
            and att.size < _TINY_IMAGE_BYTES
        ):
            decision = AttachmentDecision(att, upload=False, skip_reason="tiny-image")
        else:
            decision = AttachmentDecision(att, upload=True)
        logger.debug(
            "partition: %r mime=%s size=%s → upload=%s skip=%s",
            att.filename,
            att.mime_type,
            att.size,
            decision.upload,
            decision.skip_reason,
        )
        out.append(decision)
    return out


def _attribute_attachments(
    parent_msg: gmail_client.GmailMessage,
    extracted: list[ExtractedMessage],
    *,
    signature_first_line_hint: str | None = None,
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

    Returns `(forwarder_decisions, by_synthetic_id)`:
      - forwarder_decisions: AttachmentDecisions owned by the top-level Gmail
        message (tiny-image skips + everything not attributed elsewhere).
      - by_synthetic_id: synthetic_id → list of AttachmentDecisions attributed
        to that extracted email.
    """
    all_decisions = _partition_attachments(parent_msg.attachments)
    if not extracted:
        return all_decisions, {}

    # Walk extracted oldest-first. history_extraction emits oldest first, but
    # be defensive in case ordering ever changes.
    ordered = sorted(extracted, key=lambda e: e.date)

    # Fallback owner when no extracted block mentions the filename. Used for
    # forwards where the outer body is empty (forwarder added no commentary)
    # AND no `[image: NAME]` markers survive in any block — typical for
    # Outlook-originated HTML-only forwards. Picking the oldest non-forwarder
    # block is a better default than dumping everything on the empty
    # forwarder row.
    forwarder_email = (_addr_to_email(parent_msg.from_field) or "").lower()
    forwarder_body_is_empty = not clean_body(parent_msg.plain_body)
    fallback_synth: str | None = None
    if forwarder_body_is_empty:
        for inner in ordered:
            inner_email = (_addr_to_email(inner.from_field) or "").lower()
            if inner_email and inner_email != forwarder_email:
                fallback_synth = synthetic_message_id(
                    parent_msg.message_id, inner.from_field, inner.body
                )
                break

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
            owner_synth = fallback_synth
        if owner_synth is None:
            forwarder.append(decision)
        else:
            by_synth.setdefault(owner_synth, []).append(decision)
    return forwarder, by_synth


@dataclass
class ThreadAttachmentTracker:
    """Per-thread memory of attachments already uploaded, and where to.

    Gmail re-carries attachments on every reply (quoted MIME tree). Each later
    reply has a DIFFERENT author, so the same bytes show up again attributed to
    a new sender. Without thread-wide content dedup the same file uploads once
    per reply that quoted it.

    `links_by_sha1` maps content sha1 → the Drive `{name, url}` entries those
    bytes were uploaded to (one per matched project subfolder). An exact sha1
    match is sender-independent: identical bytes are the same file no matter who
    the quoting message is from (two distinct real files won't collide). That
    kills the re-carry *upload* duplicates — but crucially we still hand the
    stored links back so the row that quoted the file can be linked to it. A
    skip must suppress the upload, never the link.
    """

    links_by_sha1: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    # Content hashes that have already been ATTACHED to a row during THIS sync
    # pass. Distinct from `links_by_sha1` (which is also pre-seeded from the
    # durable ThreadAttachment table across syncs, so it can't tell "first
    # message" from "re-sync"). `attached_this_pass` starts empty every sync, so
    # the message that FIRST carries a file this pass attaches it, and only the
    # later messages that re-carry the same bytes skip it. This is what stops
    # every reply row from getting the whole thread's attachments.
    attached_this_pass: set[str] = field(default_factory=set)

    @property
    def sha1s(self) -> set[str]:
        """Content hashes already uploaded in this thread (read-only view)."""
        return set(self.links_by_sha1)


async def _upload_attachments(
    *,
    parent_msg: gmail_client.GmailMessage,
    decisions: list[AttachmentDecision],
    attributed_sender: str,
    user_email: str,
    session: AsyncSession,
    thread_tracker: ThreadAttachmentTracker,
    project_label_paths: list[str],
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
    quoted from an earlier message. A re-carried (sha1-known) attachment is NOT
    re-uploaded, but its stored Drive links ARE returned, so the quoting row
    still gets linked — "already on Drive" must never mean "drop from the row".
    `project_label_paths` is the list of Gmail-label paths matched for this
    thread (e.g. `["Projects/2026/Acme", "Projects/2026/Beta"]`). Each
    attachment uploads once per project into
    `<attachments_folder_name>/<label-path-segments>/`. The row's `Files`
    property is set to the union of returned URLs — Notion shows every link
    on the row regardless of which project subfolder it lives in.

    Failures are per-attachment (per-project) — one Drive error doesn't stop
    the rest. The row already exists in Notion before this is called, so
    even a total upload failure leaves a valid (file-less) row behind.
    """
    uploaded: list[dict[str, str]] = []
    sender = (attributed_sender or "").lower() or user_email.lower()
    # Pre-compute one folder path tuple per matched project. The root segment
    # (`settings.attachments_folder_name`) keeps everything under a single
    # top-level container in My Drive, with `Projects/<year>/<name>` nested
    # below — matching the Gmail label hierarchy 1:1.
    root = settings.attachments_folder_name
    folder_paths: list[tuple[str, ...]] = [
        (root, *label_path.split("/")) for label_path in project_label_paths
    ]
    if not folder_paths:
        # Defensive: every sync_thread caller arrives here only after
        # _pick_projects matched ≥1 project. If we ever wire in a no-project
        # path, fall back to the flat root rather than uploading nothing.
        folder_paths = [(root,)]
    for d in decisions:
        if not d.upload:
            logger.info(
                "    ⏏  skip attachment %r: %s", d.attachment.filename, d.skip_reason
            )
            continue
        if d.attachment.attachment_id:
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
        elif d.attachment.inline_data:
            # Inline-embedded part: Gmail returned the bytes in body.data with
            # no attachmentId. Decode the bytes we already downloaded instead
            # of making a (impossible) attachments.get call. This is the path
            # Outlook inline photos take — without it they're silently lost.
            content = gmail_client.decode_inline_data(d.attachment.inline_data)
        else:
            logger.info(
                "    ⏏  skip attachment %r: inline-embedded, no id and no data",
                d.attachment.filename,
            )
            continue
        if not content:
            logger.warning(
                "    ⏏  skip attachment %r: empty content from Gmail", d.attachment.filename
            )
            continue
        content_sha1 = hashlib.sha1(content).hexdigest()

        # Row-attribution: Gmail/Outlook re-carries identical bytes on every
        # reply's MIME tree, so the same file reappears under each later author.
        # The file belongs to the message that FIRST carried it THIS pass; a
        # reply only quoted it. If we've already attached these bytes to an
        # earlier row this pass, drop them from this row entirely. (Using a
        # per-pass set, not links_by_sha1, so a re-sync still attaches the first
        # message's files — links_by_sha1 is pre-seeded across syncs.)
        if content_sha1 in thread_tracker.attached_this_pass:
            logger.info(
                "    ⏏  skip attachment %r: already attached to an earlier message "
                "in this thread (re-carried quote)",
                d.attachment.filename,
            )
            continue

        # Upload-dedup: if these exact bytes were already sent to Drive (this
        # thread, possibly a previous sync), reuse the stored links instead of
        # re-uploading — but DO attach them to this (first-this-pass) row.
        known_links = thread_tracker.links_by_sha1.get(content_sha1)
        if known_links:
            relinked = [
                {"name": d.attachment.filename or link.get("name") or "attachment",
                 "url": link["url"]}
                for link in known_links
                if link.get("url")
            ]
            uploaded.extend(relinked)
            thread_tracker.attached_this_pass.add(content_sha1)
            logger.info(
                "    📎 linked %r from Drive (already uploaded earlier)",
                d.attachment.filename,
            )
            continue

        # Upload once per matched project subfolder. Each project's folder
        # becomes self-contained (good for archival/export), at the cost of N
        # Drive files for an N-project email — the team chose this trade-off
        # deliberately.
        att_links: list[dict[str, str]] = []
        for folder_path in folder_paths:
            try:
                url = await asyncio.to_thread(
                    drive_client.upload_attachment,
                    user_email,
                    folder_path,
                    d.attachment.filename,
                    d.attachment.mime_type,
                    content,
                )
            except Exception:
                logger.exception(
                    "    ✗ Drive upload failed for %r → %s",
                    d.attachment.filename,
                    "/".join(folder_path),
                )
                continue
            att_links.append({"name": d.attachment.filename, "url": url})
            logger.info(
                "    📎 uploaded %r (%.1f KB) from %s → %s",
                d.attachment.filename,
                len(content) / 1024,
                sender,
                "/".join(folder_path),
            )
        if att_links:
            uploaded.extend(att_links)
            thread_tracker.attached_this_pass.add(content_sha1)
            # Only record once at least one project copy made it through —
            # otherwise a transient Drive failure would prevent a retry on
            # the next thread sync. Storing the links (not just the sha1) is
            # what lets a later re-carry — which skips the upload — still link
            # its row to the file already on Drive.
            thread_tracker.links_by_sha1[content_sha1] = att_links
            # Persist sha1 + links so the next sync of this thread (a reply that
            # re-carries the same bytes) skips the re-upload yet can still link.
            # on_conflict_do_update refreshes the links if a prior row had none
            # (e.g. written before the column existed); safe under the
            # concurrent-sync races the thread lock already guards against.
            await session.execute(
                insert(ThreadAttachment)
                .values(
                    gmail_thread_id=parent_msg.thread_id,
                    content_sha1=content_sha1,
                    first_filename=(d.attachment.filename or "")[:255],
                    drive_links=att_links,
                )
                .on_conflict_do_update(
                    index_elements=["gmail_thread_id", "content_sha1"],
                    set_={"drive_links": att_links},
                )
            )
    return uploaded


async def _relink_existing_row_files(
    *,
    parent_msg: gmail_client.GmailMessage,
    decisions: list[AttachmentDecision],
    attributed_sender: str,
    row_id: str,
    user_email: str,
    session: AsyncSession,
    emails_db_props: dict[str, Any],
    thread_tracker: ThreadAttachmentTracker,
    project_label_paths: list[str],
) -> None:
    """Re-set the Files property on an ALREADY-EXISTING row (dedup-hit path).

    The row's existence is cached, so the create path (which is the only place
    that historically set Files) is skipped. Without this, a row whose files
    were never linked — e.g. created in a sync where the upload was already
    deduped, or by an older build — stays file-less forever, because the sha1
    dedup that prevents a duplicate Drive upload also prevented the re-link.

    Calling `_upload_attachments` here is safe and cheap: re-carried bytes are
    sha1-known and re-linked (no Drive upload), genuinely-new bytes upload once.
    `patch_email_row_files` overwrites the property wholesale, so repeating it
    every sync is idempotent. No-op when this message carries no uploadable
    attachments or the DB has no Files property.
    """
    if EMAILS_PROPS.get("files") not in emails_db_props:
        return
    if not any(d.upload for d in decisions):
        return
    uploaded = await _upload_attachments(
        parent_msg=parent_msg,
        decisions=decisions,
        attributed_sender=attributed_sender,
        user_email=user_email,
        session=session,
        thread_tracker=thread_tracker,
        project_label_paths=project_label_paths,
    )
    if not uploaded:
        return
    try:
        await notion_client.patch_email_row_files(row_id, uploaded)
    except Exception:
        logger.exception("Failed to re-link Files on existing row %s", row_id)


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
