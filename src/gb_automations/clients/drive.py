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


@lru_cache(maxsize=32)
def _ensure_attachments_folder(user_email: str, folder_name: str) -> str:
    """Find or create the attachments folder under this user's Drive, return its ID.

    Cached per (user, folder name) so repeated uploads inside a single sync
    don't re-query Drive. Cache survives for the process lifetime; on
    container restart the first upload re-queries (free).
    """
    service = drive_for(user_email)
    # Search by name+type, my-Drive scope, non-trashed.
    query = (
        f"name = '{folder_name}' "
        "and mimeType = 'application/vnd.google-apps.folder' "
        "and trashed = false"
    )
    logger.debug("drive → files.list(name=%r) for %s", folder_name, user_email)
    response = service.files().list(q=query, fields="files(id, name)", pageSize=5).execute()
    files = response.get("files", [])
    if files:
        folder_id = files[0]["id"]
        logger.debug("drive: existing attachments folder id=%s", folder_id)
        return folder_id
    # Create — first upload for this user.
    logger.info("drive: creating attachments folder %r in %s's Drive", folder_name, user_email)
    created = service.files().create(
        body={
            "name": folder_name,
            "mimeType": "application/vnd.google-apps.folder",
        },
        fields="id",
    ).execute()
    return created["id"]


def upload_attachment(
    user_email: str,
    folder_name: str,
    filename: str,
    mime_type: str,
    content: bytes,
) -> str:
    """Upload `content` to the named folder in `user_email`'s Drive.

    Returns a `https://drive.google.com/file/d/<id>/view` URL that anyone with
    the link can view. Notion renders this as a clickable file link.
    """
    service = drive_for(user_email)
    folder_id = _ensure_attachments_folder(user_email, folder_name)

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
