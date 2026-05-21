"""Google Drive client for storing email attachments.

Uploads each attachment to a shared "Notion Email Attachments" folder under
the authenticated user's Drive, sets "anyone with link can view" permission,
and returns the open-with-link URL. Notion's Files property uses that URL as
an external file reference (no Notion-hosted upload involved).

Uses the same service account JSON + domain-wide delegation as the Gmail
client, but with a separate scope. The service account must have
`https://www.googleapis.com/auth/drive` added in Workspace admin's DWD config
before this module will work (see docs/setup.md).
"""

import io
import logging
from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build
from googleapiclient.http import MediaIoBaseUpload

from gb_automations.config import settings

logger = logging.getLogger(__name__)

# Drive scope. Read+write to the user's Drive — required to create the
# folder, upload files, and set sharing permissions.
SCOPES = ["https://www.googleapis.com/auth/drive"]


@lru_cache(maxsize=1)
def _base_credentials() -> service_account.Credentials:
    if not settings.google_service_account_json:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not configured")
    return service_account.Credentials.from_service_account_file(
        settings.google_service_account_json, scopes=SCOPES
    )


def drive_for(user_email: str) -> Resource:
    """Build a Drive v3 client impersonating `user_email` via DWD."""
    delegated = _base_credentials().with_subject(user_email)
    return build("drive", "v3", credentials=delegated, cache_discovery=False)


def _escape_drive_query(value: str) -> str:
    # Drive v3 query strings are single-quoted; literal apostrophes (e.g. a
    # project named "O'Brien") and backslashes must be backslash-escaped or
    # the query is rejected with a 400.
    return value.replace("\\", "\\\\").replace("'", "\\'")


@lru_cache(maxsize=256)
def _ensure_folder_path(user_email: str, path_segments: tuple[str, ...]) -> str:
    """Find or create each segment top-down under user_email's My Drive, return leaf ID.

    Walks `path_segments` left→right; at each level scopes the lookup by
    `parent in parents` so two folders sharing a name in different subtrees
    (e.g. "Prosjekt/2026/Acme" vs "Prosjekt/2025/Acme") don't collide.

    Cached per (user, full path) so repeated uploads inside a sync only hit
    Drive once per unique folder. Cache survives for the process lifetime; a
    container restart re-queries on first use (free).
    """
    if not path_segments:
        raise ValueError("path_segments must be non-empty")
    service = drive_for(user_email)
    parent_id = "root"
    for depth, segment in enumerate(path_segments):
        escaped = _escape_drive_query(segment)
        query = (
            f"name = '{escaped}' "
            "and mimeType = 'application/vnd.google-apps.folder' "
            f"and '{parent_id}' in parents "
            "and trashed = false"
        )
        logger.debug(
            "drive → files.list(name=%r, parent=%s) for %s",
            segment, parent_id, user_email,
        )
        response = service.files().list(
            q=query, fields="files(id, name)", pageSize=5
        ).execute()
        files = response.get("files", [])
        if files:
            parent_id = files[0]["id"]
            continue
        # Create the missing segment; subsequent depths attach below it.
        logger.info(
            "drive: creating folder %r under parent=%s in %s's Drive",
            segment, parent_id, user_email,
        )
        body = {
            "name": segment,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = service.files().create(body=body, fields="id").execute()
        parent_id = created["id"]
    return parent_id


def upload_attachment(
    user_email: str,
    folder_path: tuple[str, ...],
    filename: str,
    mime_type: str,
    content: bytes,
) -> str:
    """Upload `content` to the nested folder path in `user_email`'s Drive.

    `folder_path` is a tuple of segment names from My Drive root to the leaf
    (e.g. `("Notion Email Attachments", "Prosjekt", "2026", "Acme")`). Missing
    segments are created on first use.

    Returns a `https://drive.google.com/file/d/<id>/view` URL that anyone with
    the link can view. Notion renders this as a clickable file link.
    """
    service = drive_for(user_email)
    folder_id = _ensure_folder_path(user_email, folder_path)

    # The Drive API wants MediaIoBaseUpload for binary content. resumable=False
    # is fine here — these are typically small (KB-MB), one-shot is faster.
    media = MediaIoBaseUpload(
        io.BytesIO(content),
        mimetype=mime_type or "application/octet-stream",
        resumable=False,
    )
    metadata = {
        "name": filename or "attachment",
        "parents": [folder_id],
    }
    logger.debug(
        "drive → files.create(name=%r, mime=%s, size=%d) parent=%s",
        filename,
        mime_type,
        len(content),
        folder_id,
    )
    created = service.files().create(
        body=metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    file_id = created["id"]

    # Make it accessible with anyone-with-link so Notion-rendered URLs work
    # for any user clicking from the email row, not just the impersonated
    # mailbox owner.
    logger.debug("drive → permissions.create(file=%s, anyone reader)", file_id)
    service.permissions().create(
        fileId=file_id,
        body={"role": "reader", "type": "anyone"},
    ).execute()

    url = created.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"
    logger.info(
        "drive ← uploaded %r (%.1f KB) → %s",
        filename,
        len(content) / 1024,
        url,
    )
    return url
