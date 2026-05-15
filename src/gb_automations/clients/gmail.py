"""Gmail client using a service account with domain-wide delegation.

`gmail_for(user_email)` returns an authenticated Gmail API resource scoped to that user.
The service account JSON path comes from settings.google_service_account_json.

Additional helpers wrap the raw Gmail payload structure (base64url bodies, nested
MIME parts, headers list) into plain Python dataclasses the sync engine can consume.
"""

import base64
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

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


def create_label(user_email: str, name: str) -> dict[str, Any]:
    """Create a user label. Idempotent: returns the existing label if one with this name exists."""
    logger.debug("gmail → labels.create(user=%s, name=%r)", user_email, name)
    existing = find_label_by_name(user_email, name)
    if existing:
        return existing
    service = gmail_for(user_email)
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


def update_label_name(user_email: str, label_id: str, new_name: str) -> dict[str, Any]:
    """Rename a Gmail label (by ID) in this user's mailbox. Returns the updated resource.

    Used when a Notion project is renamed — we patch the label rather than
    create-new + delete-old so existing threads keep their label without churn.
    """
    logger.debug(
        "gmail → labels.patch(user=%s, id=%s, name=%r)", user_email, label_id, new_name
    )
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
            historyTypes=["messageAdded", "labelAdded"],
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
            attachments.append(
                GmailAttachment(
                    filename=filename,
                    mime_type=mime,
                    size=body.get("size", 0),
                    attachment_id=body.get("attachmentId"),
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

    if plain_parts:
        body_text = "\n".join(plain_parts).strip()
    elif html_parts:
        body_text = _strip_html("\n".join(html_parts)).strip()
    else:
        body_text = ""

    return body_text, attachments


def _decode_base64url(data: str) -> str:
    """Decode Gmail's base64url-encoded body data. Tolerant of missing padding."""
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")


def _strip_html(html: str) -> str:
    """Crude HTML → text fallback for the rare message with no text/plain part."""
    import re

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
