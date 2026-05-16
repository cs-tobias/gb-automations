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
    to_field: str = ""  # raw "To"/"Til" header value, comma-separated. Populated
                        # only for messages whose boundary was a multi-line forward
                        # header (Outlook style) — inline replies like "X skrev Y:"
                        # don't carry To/Cc info. Used to build the To relation on
                        # the Notion row.
    cc_field: str = ""  # raw "Cc"/"Kopi" header value, comma-separated. Same
                        # caveats as to_field.


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


def find_under_split_blocks(extracted: list[ExtractedMessage]) -> list[int]:
    """Indices of extracted blocks that still contain un-split inner messages.

    A block is under-split when its `raw_body` contains a reply/forward
    boundary marker followed by substantive content — meaning the regex
    splitter saw the outer message correctly but missed an inner forward
    inside one of the produced blocks (typically when a client uses a
    format the regex layer doesn't recognize, e.g. a new Outlook variant).

    Detection runs on `raw_body` (pre-clean) so the markers haven't been
    stripped yet. Empty `raw_body` returns no indices for that block — the
    older test-fixture path that only populates `body` is treated as "no
    evidence of under-split", which is the safe default.

    Returns block indices in the same order as `extracted`. Callers use this
    to decide whether to invoke a more expensive (e.g. LLM-based) re-splitter
    on the offending blocks only.
    """
    # Imported here to avoid a circular import: email_cleaning is the lower
    # layer; this module is consumed by both regex and LLM splitters.
    from gb_automations.utils.email_cleaning import find_reply_boundary_ranges

    out: list[int] = []
    for i, msg in enumerate(extracted):
        if not msg.raw_body:
            continue
        inner = find_reply_boundary_ranges(msg.raw_body)
        if not inner:
            continue
        # An inner boundary alone isn't proof — it could be the last few lines
        # of the block (e.g. a Fra: header with no body that the outer extractor
        # already attributed correctly elsewhere). Require non-blank content
        # AFTER the last inner boundary's header_end_line.
        last = inner[-1]
        tail_lines = msg.raw_body.replace("\r\n", "\n").split("\n")[last.header_end_line + 1 :]
        if any(line.strip() for line in tail_lines):
            out.append(i)
    return out


def infer_missing_to_fields(extracted: list[ExtractedMessage]) -> None:
    """Fill in missing `to_field` values by walking the chain chronologically.

    Inline reply boundaries (`X skrev Y:`, `On X wrote:`) only carry the
    sender's name+email — they don't include a `To:` header. After the regex
    splitter runs, those blocks have `to_field=""`. In a chronological reply
    chain, message N's recipient is almost always message N-1's sender — so
    we fill the missing slots from the immediately-prior block's From.

    Mutates `extracted` in place. Skipped cases:
      - The oldest block (no prior to reference).
      - Blocks whose prior block was sent by the SAME person (back-to-back
        from one sender; the recipient is genuinely unknown without more
        context — better empty than guessed-wrong).
      - Blocks where the prior block had no parseable From email.
      - Blocks that already have a `to_field` populated (header carried it).
    """
    if len(extracted) < 2:
        return

    # Lazy import — participants.py is a higher-level utility that may grow
    # heavier deps; this module is the lowest layer.
    from gb_automations.utils.participants import find_sender_email

    # Iterate chronologically. `extract_history_blocks` reverses to oldest-first
    # before returning, so the input list is already in the right order.
    for i in range(1, len(extracted)):
        cur = extracted[i]
        if cur.to_field:
            continue  # explicit To from the header — never overwrite
        prev = extracted[i - 1]
        prev_email = find_sender_email(prev.from_field).lower()
        cur_email = find_sender_email(cur.from_field).lower()
        if not prev_email:
            continue  # nothing to assign
        if prev_email == cur_email:
            # Same person sent two messages in a row — recipient genuinely
            # unknown. Leave empty rather than self-reference.
            continue
        cur.to_field = prev.from_field


__all__ = [
    "ExtractedMessage",
    "find_under_split_blocks",
    "infer_missing_to_fields",
    "synthetic_message_id",
]
