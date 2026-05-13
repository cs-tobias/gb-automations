"""Gmail client using a service account with domain-wide delegation.

`gmail_for(user_email)` returns an authenticated Gmail API resource scoped to that user.
The service account JSON path comes from settings.google_service_account_json.
"""

from functools import lru_cache

from google.oauth2 import service_account
from googleapiclient.discovery import Resource, build

from gb_automations.config import settings

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
