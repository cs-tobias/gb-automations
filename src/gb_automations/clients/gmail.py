"""Gmail client using a service account with domain-wide delegation.

`gmail_for(user_email)` returns an authenticated Gmail API resource scoped to that user.
The service account JSON path comes from settings.google_service_account_json.

Additional helpers wrap the raw Gmail payload structure (base64url bodies, nested
MIME parts, headers list) into plain Python dataclasses the sync engine can consume.
"""

import base64
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from gb_automations.config import settings

logger = logging.getLogger(__name__)

# Full-Gmail scope matches what's authorized in Workspace admin (step D in setup).
SCOPES = ["https://mail.google.com/"]


@lru_cache(maxsize=1)
def _base_credentials() -> service_account.Credentials:
    if not settings.google_service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    return service_account.Credentials.from_service_account_file(
        settings.google_service_account_json, scopes=SCOPES
    )


def gmail_for(user_email: str) -> Resource:
    """Build a Gmail API client impersonating `user_email` via DWD."""
    delegated = _base_credentials().with_subject(user_email)
    # cache_discovery=False avoids a noisy warning when running without a writable disk cache.
    return build("gmail", "v1", credentials=delegated, cache_discovery=False)


# ============================================================
# Dataclasses for the sync engine
# ============================================================


@dataclass
class GmailAttachment:
    filename: str
    mime_type: str
    size: int
    attachment_id: str | None  # None for inline/binary-included parts
    content_id: str | None = None  # MIME Content-ID without `<>`, lowercased.
    # Used to resolve `<img src="cid:...">` references back to the attachment
    # when an HTML-only message is processed (Outlook forwards, mostly).
    inline_ref_count: int = 0  # How many `<img src="cid:...">` references
    # to this attachment exist in the message's HTML alternative. >=2 within
    # a SINGLE Gmail message means a signature decoration repeated per
    # quoted block (Outlook keeps the inline logo in every reply level).
    # Real photo attachments are referenced exactly once.


@dataclass
class GmailMessage:
    message_id: str
    thread_id: str
    date: datetime  # UTC, from Gmail's internalDate (always reliable)
    subject: str
    from_field: str  # raw "Name <email>" header value
    to_field: str
    cc_field: str
    plain_body: str
    attachments: list[GmailAttachment]
    label_ids: list[str]  # Gmail label IDs applied to this message


@dataclass
class GmailThread:
    thread_id: str
    messages: list[GmailMessage]


# ============================================================
# Fetch helpers
# ============================================================


def get_thread(user_email: str, thread_id: str) -> GmailThread:
    """Fetch a full thread with all messages decoded into GmailMessage dataclasses."""
    logger.debug("gmail → threads.get(user=%s, id=%s)", user_email, thread_id)
    service = gmail_for(user_email)
    raw = service.users().threads().get(userId="me", id=thread_id, format="full").execute()
    messages = [_parse_message(m) for m in raw.get("messages", [])]
    return GmailThread(thread_id=raw["id"], messages=messages)


def get_message(user_email: str, message_id: str) -> GmailMessage:
    """Fetch one message by ID, fully decoded."""
    logger.debug("gmail → messages.get(user=%s, id=%s)", user_email, message_id)
    service = gmail_for(user_email)
    raw = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return _parse_message(raw)


def get_attachment_bytes(user_email: str, message_id: str, attachment_id: str) -> bytes:
    """Download an attachment's binary content via messages.attachments.get.

    Returns raw bytes. The Gmail API returns base64url-encoded data; we decode
    it here so callers get clean binary they can hand to Drive or hash directly.
    """
    logger.debug(
        "gmail → messages.attachments.get(user=%s, msg=%s, att=%s)",
        user_email,
        message_id,
        attachment_id,
    )
    service = gmail_for(user_email)
    response = (
        service.users()
        .messages()
        .attachments()
        .get(userId="me", messageId=message_id, id=attachment_id)
        .execute()
    )
    data = response.get("data", "")
    return base64.urlsafe_b64decode(data) if data else b""


# ============================================================
# Labels
# ============================================================


def list_labels(user_email: str) -> list[dict[str, str]]:
    logger.debug("gmail → labels.list(user=%s)", user_email)
    service = gmail_for(user_email)
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    return [{"id": label["id"], "name": label["name"], "type": label["type"]} for label in labels]


def find_label_by_name(user_email: str, name: str) -> dict[str, str] | None:
    for label in list_labels(user_email):
        if label["name"] == name:
            return label
    return None


def get_label(user_email: str, label_id: str) -> dict[str, Any]:
    """Fetch a single label by ID. Raises HttpError(404) if it doesn't exist.

    Cheaper than list_labels when we just want one — used by the button handler
    to detect Gmail-side renames (live label name diverged from what Notion
    expects) without scanning every label in the mailbox.
    """
    logger.debug("gmail → labels.get(user=%s, id=%s)", user_email, label_id)
    service = gmail_for(user_email)
    return service.users().labels().get(userId="me", id=label_id).execute()


def _create_single_label(service: Any, name: str) -> dict[str, Any]:
    """One labels.create call — does NOT pre-create parents. Internal."""
    return (
        service.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute()
    )


def create_label(user_email: str, name: str) -> dict[str, Any]:
    """Create a user label, pre-creating every parent in a nested path.

    Idempotent: if the full label already exists, returns it without API calls.

    The returned dict carries an extra `_created` boolean: True when this call
    actually minted the leaf label, False when the leaf was already present
    (the idempotent no-op path). Callers use this to log "created" vs.
    "already present" honestly instead of counting every successful call as
    a create.

    Gmail uses `/` as the hierarchy separator in label names, BUT
    `users.labels.create` does NOT auto-create parent labels — if you POST
    "Projects/2026/Acme" without "Projects" and "Projects/2026" existing
    first, Gmail creates a single flat label whose literal name contains the
    `/` characters. We walk the path top-down and create each missing prefix.
    See docs/gotchas.md.
    """
    logger.debug("gmail → labels.create(user=%s, name=%r)", user_email, name)

    # Snapshot the user's labels once; reuse for every prefix lookup so a
    # 3-level path doesn't fan out into 3 separate labels.list calls.
    existing_by_name = {label["name"]: label for label in list_labels(user_email)}
    if name in existing_by_name:
        return {**existing_by_name[name], "_created": False}

    service = gmail_for(user_email)
    parts = name.split("/")
    last_label: dict[str, Any] | None = None
    leaf_created = False
    for i in range(1, len(parts) + 1):
        prefix = "/".join(parts[:i])
        is_leaf = i == len(parts)
        if prefix in existing_by_name:
            last_label = existing_by_name[prefix]
            continue
        try:
            last_label = _create_single_label(service, prefix)
            if is_leaf:
                leaf_created = True
        except HttpError as err:
            # 409 = a concurrent webhook just created this prefix. Re-fetch
            # the labels and continue — treat as success, not a failure.
            if err.resp.status == 409:
                logger.info(
                    "label %r already existed at create time (409); fetching", prefix
                )
                refreshed = find_label_by_name(user_email, prefix)
                if refreshed is None:
                    raise
                last_label = refreshed
            else:
                raise
        existing_by_name[prefix] = last_label  # type: ignore[assignment]

    assert last_label is not None  # parts is non-empty so the loop runs at least once
    return {**last_label, "_created": leaf_created}


def update_label_name(user_email: str, label_id: str, new_name: str) -> dict[str, Any]:
    """Rename a Gmail label (by ID) in this user's mailbox. Returns the updated resource.

    Used when a Notion project is renamed — we patch the label rather than
    create-new + delete-old so existing threads keep their label without churn.

    If `new_name` is a nested path ("Projects/2026/Acme") and any parent
    doesn't exist yet, pre-create it. Same gotcha as create_label: Gmail
    would otherwise rename the label to a flat name with literal slashes.
    """
    logger.debug(
        "gmail → labels.patch(user=%s, id=%s, name=%r)", user_email, label_id, new_name
    )
    if "/" in new_name:
        # Ensure every parent prefix exists so Gmail renders the rename as
        # a hierarchy move, not a flat name with literal slashes.
        parent = new_name.rsplit("/", 1)[0]
        create_label(user_email, parent)

    service = gmail_for(user_email)
    return (
        service.users()
        .labels()
        .patch(userId="me", id=label_id, body={"name": new_name})
        .execute()
    )


def add_label_to_thread(user_email: str, thread_id: str, label_name: str) -> None:
    """Add a label (by name) to a thread. Used for testing / manual ops."""
    label = create_label(user_email, label_name)  # ensures it exists
    service = gmail_for(user_email)
    service.users().threads().modify(
        userId="me", id=thread_id, body={"addLabelIds": [label["id"]]}
    ).execute()


def list_threads_with_label(
    user_email: str, label_name: str, max_results: int = 30
) -> list[dict[str, str]]:
    """Return a list of {id, snippet, historyId} for threads carrying the given label."""
    service = gmail_for(user_email)
    response = (
        service.users()
        .threads()
        .list(userId="me", q=f'label:"{label_name}"', maxResults=max_results)
        .execute()
    )
    return response.get("threads", [])


# ============================================================
# Pub/Sub push: watch + history (Stage 4c)
# ============================================================


def start_watch(user_email: str, topic_name: str) -> dict[str, Any]:
    """Start a Gmail push-notification watch on a mailbox.

    Returns {"historyId": "<str>", "expiration": "<ms_since_epoch_str>"}.
    Watch tokens expire after ~7 days — must be renewed before then.
    """
    service = gmail_for(user_email)
    # INBOX covers incoming mail; SENT covers replies the user sends out from
    # this mailbox. Both can land on a labeled project thread, both need to
    # surface in Notion. The actual project-label filtering happens in
    # sync_thread() server-side, so this filter is intentionally coarse.
    body = {
        "topicName": topic_name,
        "labelFilterBehavior": "INCLUDE",
        "labelIds": ["INBOX", "SENT"],
    }
    return service.users().watch(userId="me", body=body).execute()


def stop_watch(user_email: str) -> None:
    """Stop the active Gmail watch for a user (cancels notifications)."""
    service = gmail_for(user_email)
    service.users().stop(userId="me").execute()


def list_history(user_email: str, start_history_id: str, max_results: int = 100) -> dict[str, Any]:
    """Fetch Gmail history starting from `start_history_id`.

    Returns the raw response; caller iterates `history` entries to find affected
    messages/threads. `historyId` on the response is the new cursor to save.
    """
    logger.debug(
        "gmail → history.list(user=%s, since=%s)", user_email, start_history_id
    )
    service = gmail_for(user_email)
    return (
        service.users()
        .history()
        .list(
            userId="me",
            startHistoryId=start_history_id,
            historyTypes=["messageAdded", "labelAdded", "labelRemoved"],
            maxResults=max_results,
        )
        .execute()
    )


# ============================================================
# Internal: parse Gmail's nested message payload
# ============================================================


def _parse_message(raw: dict[str, Any]) -> GmailMessage:
    """Convert Gmail's raw API payload into a flat GmailMessage."""
    headers = _headers_dict(raw.get("payload", {}).get("headers", []))
    body, attachments = _extract_body_and_attachments(raw.get("payload", {}))
    # internalDate is ms since epoch, always UTC.
    internal_ms = int(raw.get("internalDate", "0"))
    return GmailMessage(
        message_id=raw["id"],
        thread_id=raw["threadId"],
        date=datetime.fromtimestamp(internal_ms / 1000, tz=UTC),
        subject=headers.get("Subject", "(no subject)"),
        from_field=headers.get("From", ""),
        to_field=headers.get("To", ""),
        cc_field=headers.get("Cc", ""),
        plain_body=body,
        attachments=attachments,
        label_ids=raw.get("labelIds", []) or [],
    )


def _headers_dict(headers: list[dict[str, str]]) -> dict[str, str]:
    return {h["name"]: h["value"] for h in headers}


def _extract_body_and_attachments(
    payload: dict[str, Any],
) -> tuple[str, list[GmailAttachment]]:
    """Walk a Gmail payload tree, return (plain_text_body, attachments_list).

    Preference order for the body:
      1. The first text/plain part found in a top-level multipart structure.
      2. text/html stripped of tags as a fallback.
    Attachments include any part with a filename, even inline images.
    """
    plain_parts: list[str] = []
    html_parts: list[str] = []
    attachments: list[GmailAttachment] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        filename = part.get("filename") or ""
        body = part.get("body", {})

        if filename:
            part_headers = _headers_dict(part.get("headers", []))
            raw_cid = part_headers.get("Content-ID") or part_headers.get("Content-Id") or ""
            attachments.append(
                GmailAttachment(
                    filename=filename,
                    mime_type=mime,
                    size=body.get("size", 0),
                    attachment_id=body.get("attachmentId"),
                    content_id=_normalize_content_id(raw_cid),
                )
            )

        data = body.get("data")
        if data and not filename:
            decoded = _decode_base64url(data)
            if mime == "text/plain":
                plain_parts.append(decoded)
            elif mime == "text/html":
                html_parts.append(decoded)

        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)

    # Count inline cid references against the joined HTML. The COUNT is the
    # signature signal (>=2 within a single Gmail message means signature
    # decoration carried per quoted reply block); the body-rendering path is
    # separate.
    if html_parts:
        _count_inline_image_refs("\n".join(html_parts), attachments)

    if html_parts:
        # Prefer HTML when present (even if text/plain is also there).
        # Reason: Outlook's auto-generated text/plain drops inline image
        # references entirely, so building the body from HTML is the only
        # way to preserve `[image: filename]` anchors at the positions
        # where the original sender embedded each image. We replace each
        # `<img src="cid:...">` with `[image: filename]` BEFORE stripping
        # tags so document order is preserved, then strip the rest.
        prepared = _inject_inline_image_markers("\n".join(html_parts), attachments)
        body_text = _strip_html(prepared).strip()
    elif plain_parts:
        # No HTML available — fall back to text/plain. Older clients and
        # some automated senders skip text/html entirely.
        body_text = "\n".join(plain_parts).strip()
    else:
        body_text = ""

    return body_text, attachments


def _count_inline_image_refs(
    html: str, attachments: list[GmailAttachment]
) -> None:
    """Populate `inline_ref_count` on each attachment by scanning the HTML.

    Mutates `attachments` in place. Resolution mirrors
    `_inject_inline_image_markers`: cid → content_id, then Outlook-style
    cid → filename-prefix fallback. Unresolvable srcs are ignored.

    Idempotent — counts are reset before counting so callers can re-run
    without doubling up.
    """
    for att in attachments:
        att.inline_ref_count = 0
    if not html or not attachments:
        return

    by_cid = {att.content_id: att for att in attachments if att.content_id}
    by_name = sorted(
        (att for att in attachments if att.filename),
        key=lambda a: len(a.filename),
        reverse=True,
    )

    for match in _IMG_SRC_RE.finditer(html):
        src = match.group(1)
        if not src.lower().startswith("cid:"):
            continue
        token = src[4:].strip().lower()
        target = by_cid.get(token)
        if target is None:
            for att in by_name:
                if token.startswith(att.filename.lower()):
                    target = att
                    break
        if target is not None:
            target.inline_ref_count += 1


def _normalize_content_id(raw: str) -> str | None:
    """Strip the `<...>` wrapper Gmail sometimes returns on Content-ID headers.

    RFC 2392 cid: URLs reference the bare token; the MIME header conventionally
    wraps it in angle brackets. Lowercased for case-insensitive matching.
    """
    if not raw:
        return None
    return raw.strip().lstrip("<").rstrip(">").lower() or None


# Document-order pattern for inline images in HTML. Captures whatever's inside
# the src="..." attribute on an <img> tag — Outlook/Word emit `cid:NAME@host`,
# while some clients emit a bare filename or `data:` URL we just leave alone.
_IMG_SRC_RE = re.compile(
    r"<img\b[^>]*?\bsrc=\"([^\"]+)\"[^>]*>",
    flags=re.IGNORECASE,
)


def _inject_inline_image_markers(
    html: str, attachments: list[GmailAttachment]
) -> str:
    """Replace each `<img src="cid:...">` with `[image: filename]` in place.

    Resolution order for each tag:
      1. `cid:TOKEN` → match by attachment.content_id (case-insensitive).
      2. `cid:TOKEN` whose token starts with a known attachment filename
         (Outlook style: `cid:image001.png@01D...` where the filename prefixes
         the cid token).

    Unresolvable srcs (data: URLs, http: URLs, broken cids) are left untouched
    so `_strip_html` discards them as before. This keeps the behavior identical
    for non-attachment image references.
    """
    if not html or not attachments:
        return html

    by_cid = {att.content_id: att for att in attachments if att.content_id}
    # Sorted by filename length desc so longer names match before shorter
    # prefixes — e.g. `image10.png` is checked before `image1.png`.
    by_name = sorted(
        (att for att in attachments if att.filename),
        key=lambda a: len(a.filename),
        reverse=True,
    )

    def resolve(src: str) -> GmailAttachment | None:
        if not src.lower().startswith("cid:"):
            return None
        token = src[4:].strip().lower()
        if token in by_cid:
            return by_cid[token]
        for att in by_name:
            if token.startswith(att.filename.lower()):
                return att
        return None

    def replace(match: re.Match[str]) -> str:
        att = resolve(match.group(1))
        if att is None:
            return match.group(0)
        # Surround with newlines so the marker survives Gmail-style flattening
        # and lands on its own line for downstream regex scanning.
        return f"\n[image: {att.filename}]\n"

    return _IMG_SRC_RE.sub(replace, html)


def _decode_base64url(data: str) -> str:
    """Decode Gmail's base64url-encoded body data. Tolerant of missing padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Crude HTML → text fallback for the rare message with no text/plain part."""
    # Drop scripts/styles entirely
    cleaned = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Convert <br>, <p>, <li> to newlines BEFORE stripping all tags
    cleaned = re.sub(r"</?(br|p|li|div|tr)\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    # Strip all remaining tags
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    # Decode common HTML entities
    cleaned = (
        cleaned.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    return cleaned
