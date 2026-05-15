"""Shared dataclass + content-hashed ID helper for extracted email records.

Both the historical LLM splitter (now removed) and the current regex-based
`utils/history_extraction.py` produce `ExtractedMessage` records. Keeping the
shape stable here means downstream sync code (`_sync_forwarded_chain`,
`_sync_extracted_message`, `_build_extracted_row_properties`) is decoupled
from how the records were produced.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass
class ExtractedMessage:
    from_field: str   # raw "Name <email>" form, or bare email, or bare name
    date: datetime    # tz-aware datetime
    subject: str      # may be "" — caller falls back to a parent-derived placeholder
    body: str         # this segment's own content; may be empty
    raw_body: str = ""  # pre-clean body slice — preserves [image: foo.png] markers
                        # that `body` (clean_body output) strips. Used by attachment
                        # attribution to detect which historical email mentions a
                        # given filename. Empty for callers that don't populate it.


def synthetic_message_id(parent_message_id: str, from_field: str, body: str) -> str:
    """Deterministic content-derived ID for an extracted sub-message.

    Format: `{parent_id}#fwd-{sha1(from + body)[:10]}`. Same `(from, body)`
    always produces the same ID — so re-running extraction on the same input
    yields the same IDs and dedup hits silently. Different content produces
    different IDs (correct: they represent different extractions).

    The `#` separator cannot occur in real Gmail message IDs (alphanumeric
    only), so a synthetic ID never collides with a real one.
    """
    digest = hashlib.sha1(f"{from_field}\n{body}".encode()).hexdigest()[:10]
    return f"{parent_message_id}#fwd-{digest}"


__all__ = [
    "ExtractedMessage",
    "synthetic_message_id",
]
