"""Deterministic regex-based splitter for forwarded email chains.

Replaces the LLM-based `split_forwarded_chain` with pure parsing. The key
insight: every distinct prior email in a Gmail body is preceded by a structural
boundary marker (an inline reply line like "X skrev Y:" or a multi-line forward
header block). Finding the boundaries is regex work; the boundary line ITSELF
names the sender, date, and (for forward blocks) subject. No model judgment is
needed for splitting — only for downstream semantic tasks (tagging, etc.).

Architectural rule for this codebase:
  - Regex = structural / mechanical work (splitting, parsing, deduping)
  - LLM   = judgment / semantic work (tagging, summarization, classification)

Public surface: `extract_history_blocks(raw_body, parent_subject, parent_date)`
returns a list of `ExtractedMessage` (oldest first, suitable for direct hand-off
to the existing `_sync_forwarded_chain` path).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from gb_automations.utils.email_cleaning import (
    _FORWARD_HEADER_LINE,
    clean_body,
    find_reply_boundary_ranges,
)
from gb_automations.utils.email_splitting import ExtractedMessage

logger = logging.getLogger(__name__)


# ============================================================
# Header parsing — pulls (name, email, date_text, subject) from each boundary
# ============================================================


@dataclass
class ParsedHeader:
    """Fields extracted from a boundary's header text. Any field may be empty."""

    name: str = ""
    email: str = ""
    date_text: str = ""    # raw date string, to be parsed by _parse_human_date
    subject: str = ""


# Inline reply-header patterns. `<email>` is OPTIONAL so the parser still works
# on bare-name headers like "On May 13, John wrote:" or "12. mai skrev Anne:".
# When email is missing, the email group is None and we leave the field empty.
#
# Generic Norwegian "<date> skrev <name>[<email>]:" — covers both Gmail formats
# (with/without "kl.", with/without comma) since the date prefix is whatever
# comes before "skrev".
_INLINE_REPLY_SKREV = re.compile(
    r"^(?P<date>.+?)\s+skrev\s+(?P<name>.+?)(?:\s*<(?P<email>[^>]+)>)?\s*:?\s*$",
    re.IGNORECASE,
)

# Norwegian "Den 13. mai 2026 skrev Anne [<a@x.com>]:"
_INLINE_REPLY_DEN_SKREV = re.compile(
    r"^Den\s+(?P<date>.+?)\s+skrev\s+(?P<name>.+?)(?:\s*<(?P<email>[^>]+)>)?\s*:?\s*$",
    re.IGNORECASE,
)

# English "On May 13, 2026, John Doe [<john@x.com>] wrote:"
# The date portion can itself contain commas ("May 13, 2026, at 2:30 PM"), so
# we anchor on the last comma before the name. Greedy `.+,` consumes the date
# all the way up to (and including) its final comma; the name is what's left
# before " wrote:".
_INLINE_REPLY_ON_WROTE = re.compile(
    r"^On\s+(?P<date>.+),\s+(?P<name>.+?)(?:\s*<(?P<email>[^>]+)>)?\s+wrote:\s*$",
    re.IGNORECASE,
)

# Multi-line forward header: each line is `Field: value`.
_FORWARD_FIELD_PATTERNS: dict[str, re.Pattern[str]] = {
    "from": re.compile(r"^(?:Fra|From):\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "date": re.compile(r"^(?:Date|Sendt|Sent|Dato):\s*(?P<value>.+?)\s*$", re.IGNORECASE),
    "subject": re.compile(r"^(?:Subject|Emne):\s*(?P<value>.+?)\s*$", re.IGNORECASE),
}

# "Name <email@host>" — extract name + email from a forward-block From: value.
_NAME_AND_EMAIL = re.compile(r"^(?P<name>.+?)\s*<(?P<email>[^>]+)>\s*$")


def parse_header(header_text: str) -> ParsedHeader:
    """Extract whatever name/email/date/subject we can from a boundary's header.

    Tries the inline-reply patterns first (one-line headers like "X skrev Y:"),
    then falls back to multi-line forward-header field parsing. Returns
    `ParsedHeader()` with empty strings if nothing matched — caller handles
    the partial case.
    """
    if not header_text:
        return ParsedHeader()

    # Inline single-line patterns: try each on the first line of the header.
    first_line = header_text.split("\n", 1)[0].strip()
    # Den-prefixed and On-prefixed patterns are MORE specific (require their
    # opener word), so try them first; the generic skrev pattern is the
    # catch-all and would also match Den-prefixed lines.
    for pattern in (_INLINE_REPLY_DEN_SKREV, _INLINE_REPLY_ON_WROTE, _INLINE_REPLY_SKREV):
        m = pattern.match(first_line)
        if m:
            email_match = m.group("email")
            return ParsedHeader(
                name=m.group("name").strip().strip("\"'").strip(),
                email=email_match.strip().lower() if email_match else "",
                date_text=m.group("date").strip(),
            )

    # Multi-line forward header: walk every line and pull out labeled fields.
    parsed = ParsedHeader()
    for line in header_text.split("\n"):
        stripped = line.strip()
        if not stripped:
            continue
        for field, pattern in _FORWARD_FIELD_PATTERNS.items():
            m = pattern.match(stripped)
            if not m:
                continue
            value = m.group("value").strip()
            if field == "from":
                # Try to split "Name <email>" into parts.
                nm = _NAME_AND_EMAIL.match(value)
                if nm:
                    parsed.name = nm.group("name").strip().strip("\"'").strip()
                    parsed.email = nm.group("email").strip().lower()
                else:
                    # Bare name or bare email — best-effort.
                    if "@" in value:
                        parsed.email = value.lower()
                    else:
                        parsed.name = value
            elif field == "date":
                parsed.date_text = value
            elif field == "subject":
                parsed.subject = value
            break
    return parsed


# ============================================================
# Date parsing — Norwegian + English month names, several formats
# ============================================================


# 3-letter and 3-or-4-letter Norwegian abbreviations, plus full English.
_MONTHS: dict[str, int] = {
    # Norwegian (abbrev + full)
    "jan": 1, "januar": 1,
    "feb": 2, "februar": 2,
    "mar": 3, "mars": 3,
    "apr": 4, "april": 4,
    "mai": 5,
    "jun": 6, "juni": 6,
    "jul": 7, "juli": 7,
    "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "september": 9,
    "okt": 10, "oktober": 10,
    "nov": 11, "november": 11,
    "des": 12, "desember": 12,
    # English (abbrev + full)
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6,
    "july": 7, "october": 10, "december": 12,
}


# Matches "fre. 8. mai 2026 kl. 13:55", "tor. 30. apr 2026 14:21", "8. mai",
# "May 13, 2026 14:30", etc. Year and time are optional. The `ampm` group
# captures AM/PM when present (English clients) for 12h→24h conversion.
_DATE_PATTERN = re.compile(
    r"(?:[a-zæøåA-ZÆØÅ]{2,9}\.?\s+)?"                          # optional weekday
    r"(?P<day>\d{1,2})\.?"                                       # day
    r"\s+"
    r"(?P<month>[a-zæøåA-ZÆØÅ]{3,9})\.?"                        # month name
    r"(?:\s+(?P<year>\d{4}))?"                                   # optional year
    r"(?:[,\s]+(?:kl\.?\s*)?(?P<hour>\d{1,2})[:.](?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])?)?",
)

# English form: "May 13, 2026, 14:30" / "May 13, 2026 at 2:30 PM"
_DATE_PATTERN_EN = re.compile(
    r"(?P<month>[a-zA-Z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s+(?P<year>\d{4})"
    r"(?:[,\s]+(?:at\s+)?(?P<hour>\d{1,2})[:.](?P<minute>\d{2})\s*(?P<ampm>[AaPp][Mm])?)?",
)


def _to_24h(hour: int, ampm: str | None) -> int:
    """Convert 12h+AM/PM to 24h. Returns hour unchanged when no AM/PM tag."""
    if not ampm:
        return hour
    is_pm = ampm.lower().startswith("p")
    if is_pm and hour < 12:
        return hour + 12
    if not is_pm and hour == 12:
        return 0
    return hour

# Norway is the relevant timezone for this codebase (Goldbox is a Norwegian
# studio). Match the parent_date's tzinfo when available; otherwise default
# to Europe/Oslo (which is UTC+1/+2 depending on DST — we use a fixed +01:00
# since we don't ship pytz/zoneinfo just for this).
_OSLO_TZ = timezone(timedelta(hours=1))


def _parse_human_date(text: str, *, fallback: datetime) -> datetime | None:
    """Parse a human-written date like 'fre. 8. mai 2026 kl. 13:55'.

    Returns None if no recognizable date pattern matches. Year falls back to
    the fallback datetime's year; time falls back to 12:00 (rough midday) so
    we don't anchor everything to midnight when only a date was given.
    Returns a tz-aware datetime, matching the fallback's tzinfo (or Oslo if
    fallback is naive).
    """
    if not text:
        return None
    tz = fallback.tzinfo or _OSLO_TZ

    # Try Norwegian/numeric-first pattern first.
    m = _DATE_PATTERN.search(text)
    if m and m.group("month").lower().rstrip(".") in _MONTHS:
        day = int(m.group("day"))
        month = _MONTHS[m.group("month").lower().rstrip(".")]
        year = int(m.group("year")) if m.group("year") else fallback.year
        hour = _to_24h(int(m.group("hour")), m.group("ampm")) if m.group("hour") else 12
        minute = int(m.group("minute")) if m.group("minute") else 0
        try:
            return datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            pass

    # English pattern: "May 13, 2026"
    m = _DATE_PATTERN_EN.search(text)
    if m and m.group("month").lower().rstrip(".") in _MONTHS:
        day = int(m.group("day"))
        month = _MONTHS[m.group("month").lower().rstrip(".")]
        year = int(m.group("year"))
        hour = _to_24h(int(m.group("hour")), m.group("ampm")) if m.group("hour") else 12
        minute = int(m.group("minute")) if m.group("minute") else 0
        try:
            return datetime(year, month, day, hour, minute, tzinfo=tz)
        except ValueError:
            pass
    return None


# ============================================================
# Main entry point
# ============================================================


# Quote prefix at the start of a line: one or more `>` chars, then optional ws.
_LINE_QUOTE_PREFIX = re.compile(r"^(\s*>+\s*)")


def _unwrap_bracketed_lines(text: str) -> str:
    """Rejoin reply-header lines that Gmail's plain-text renderer wrapped.

    Gmail wraps plain-text content at ~76 chars, which sometimes splits a
    header line right inside `<email@host>`:

        tor. 30. apr. 2026 kl. 14:21 skrev Heidi T Bekkevold <
        htb@metropolis.no>: Hei, sender over en wetransfer.

    Deeply quoted history multiplies the trouble — in a nested reply chain,
    the wrapped line and its continuation both carry the SAME `>` quote
    prefix (e.g. `>>>>>>>>>>>>>>>> tor. ... <` and `>>>>>>>>>>>>>>>> htb@...>:`).
    The continuation's quote prefix must be stripped before joining or the
    `>`s end up embedded mid-line, corrupting the email address.

    After joining `<email>` across lines, if the joined line still contains
    body content AFTER the closing `>:` (or `>: Hei, ...`), we split that
    content onto a new line so the boundary marker stays single-line.

    Rule:
      1. If a line has more `<` than `>` (unclosed bracket), strip the SAME
         quote-prefix from the next line and join.
      2. If the merged line has body content after `>:`, split it off.
      3. Cap at 3 joins per logical line.
    """
    if not text or "<" not in text:
        return text
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i]
        # 1) Join wrapped lines until brackets are balanced, or we've folded
        # a `Cc:`/`To:`-style list that wrapped on a trailing comma.
        joins = 0
        while joins < 3 and i + 1 < len(lines) and _should_join_with_next(current, lines[i + 1]):
            i += 1
            next_line = _strip_matching_quote_prefix(current, lines[i])
            current = current.rstrip() + " " + next_line.lstrip()
            joins += 1
        # 2) If we joined and the merged line now has content AFTER the `>:`
        # boundary terminator, split it so the marker line stands alone and
        # the body text becomes its own line. Without this step the boundary
        # regex would swallow body content into the name/email field.
        if joins > 0:
            current = _split_after_marker_terminator(current)
        out.append(current)
        i += 1
    return "\n".join(out)


def _should_join_with_next(current: str, next_line: str) -> bool:
    """True iff `current` is a continuation-prone line that wraps into `next_line`.

    Two cases handle real Gmail wrap patterns:
      A. `current` has an unclosed `<...>` bracket — almost always a wrapped
         `<email@host>` that continues on the next line.
      B. `current` matches a forward-header pattern (From/Fra/To/Cc/Subject/...)
         AND ends with `,` — a wrapped recipient list. Joining keeps the
         entire list on one logical line so boundary detection treats the
         whole forward-header block as one boundary.

    Both cases require `next_line` to be non-blank — a blank line always
    terminates the wrap.
    """
    if not next_line.strip():
        return False
    if _has_unclosed_bracket(current):
        return True
    cur_stripped = _LINE_QUOTE_PREFIX.sub("", current).strip()
    if cur_stripped.endswith(",") and _FORWARD_HEADER_LINE.match(cur_stripped):
        return True
    return False


def _has_unclosed_bracket(line: str) -> bool:
    """True if `line` has more `<` chars than `>` chars (open bracket pending).

    Strips the leading quote prefix first so `>>>> foo <` doesn't count the
    quote `>`s against the body's `<`.
    """
    m = _LINE_QUOTE_PREFIX.match(line)
    body = line[m.end():] if m else line
    return body.count("<") > body.count(">")


def _strip_matching_quote_prefix(current: str, next_line: str) -> str:
    """Strip `next_line`'s leading quote prefix iff it matches `current`'s.

    When Gmail wraps a quoted line, the continuation inherits the same `>`
    level. Removing it gives us a clean continuation to append.

    If quote levels differ (e.g. current is `>>` but next is `>>>`), the
    continuation isn't really a wrap — leave it alone.
    """
    cur_m = _LINE_QUOTE_PREFIX.match(current)
    nxt_m = _LINE_QUOTE_PREFIX.match(next_line)
    if not cur_m or not nxt_m:
        return next_line
    cur_prefix = cur_m.group(1).strip()  # the `>`s, ignoring spaces
    nxt_prefix = nxt_m.group(1).strip()
    if cur_prefix == nxt_prefix:
        return next_line[nxt_m.end():]
    return next_line


# Matches a closing email-bracket + colon optionally followed by body content.
# `>` + optional whitespace + `:` + optional whitespace + body text.
_MARKER_TERMINATOR = re.compile(r"(>\s*:)\s*(\S.*)$")


def _split_after_marker_terminator(line: str) -> str:
    """If `line` has `>: <content>` in it, split content onto a new line.

    Example:
        "tor. 30. apr. skrev Heidi T Bekkevold <htb@x.com>: Hei, sender..."
        →
        "tor. 30. apr. skrev Heidi T Bekkevold <htb@x.com>:
        Hei, sender..."

    Only fires when the marker terminator (`>:`) is followed by non-whitespace
    content on the same line. Leaves clean marker lines alone.
    """
    m = _MARKER_TERMINATOR.search(line)
    if not m:
        return line
    # Keep the `>:` on the marker line; put the trailing content on a new line.
    marker_end = m.start(2)
    return line[:marker_end].rstrip() + "\n" + line[marker_end:]


def extract_history_blocks(
    raw_body: str,
    parent_subject: str,
    parent_date: datetime,
) -> list[ExtractedMessage]:
    """Split a body into per-author blocks and return one ExtractedMessage each.

    Returns blocks in CHRONOLOGICAL order (oldest first). Empty list if no
    boundaries are detected.

    For each detected boundary:
      1. Parse the boundary's header to get name/email/date/subject.
      2. Take the lines between this boundary's `header_end_line` and the next
         boundary's `header_start_line` (or end-of-body) as the raw block body.
      3. Run `clean_body` on the block body to strip nested quotes and signatures.
      4. Skip the block if both name/email and body are empty (pure noise).
    """
    if not raw_body:
        return []

    # Unwrap Gmail-wrapped `<email>` and header-list lines so reply markers stay
    # on one line — boundary detection and header parsing both depend on this.
    raw_body = _unwrap_bracketed_lines(raw_body)

    normalized_lines = raw_body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    boundaries = find_reply_boundary_ranges(raw_body)
    if not boundaries:
        return []

    out: list[ExtractedMessage] = []
    for i, boundary in enumerate(boundaries):
        body_start = boundary.header_end_line + 1
        body_end = boundaries[i + 1].header_start_line if i + 1 < len(boundaries) else len(
            normalized_lines
        )
        raw_block_body = "\n".join(normalized_lines[body_start:body_end])
        # clean_body strips nested quotes (lines starting with >), signatures
        # ("Mvh", etc.), and inline noise. Result is just this author's words.
        cleaned = clean_body(raw_block_body)

        header = parse_header(boundary.header_text)
        # Fallbacks: name might be empty if parsing failed. Use the label as
        # last resort so the Notion row still has *something* identifying.
        from_field = _format_from_field(header.name, header.email)
        if not from_field and not cleaned:
            # Pure noise boundary (couldn't parse header AND no body content).
            continue

        # Date: prefer the parsed date; fall back to parent_date minus an offset
        # so chronological ordering is preserved among sibling blocks.
        parsed_date = _parse_human_date(header.date_text, fallback=parent_date)
        if parsed_date is None:
            # Offset by (i+1) seconds so blocks remain orderable in their
            # source order; we reverse to chronological at the end.
            parsed_date = parent_date - timedelta(seconds=i + 1)

        out.append(
            ExtractedMessage(
                from_field=from_field,
                date=parsed_date,
                subject=header.subject or _fwd_subject(parent_subject),
                body=cleaned,
                raw_body=raw_block_body,
            )
        )

    # Boundaries are detected top-to-bottom in the body. In standard Gmail
    # reply convention that's newest-first, so reverse to chronological
    # (oldest-first) order for downstream consumption.
    out.reverse()
    return out


def _format_from_field(name: str, email: str) -> str:
    """Render a 'Name <email>' or fallback form, matching Gmail's header style."""
    if name and email:
        return f"{name} <{email}>"
    if email:
        return email
    return name  # may be empty — caller decides


def _fwd_subject(parent_subject: str) -> str:
    """Placeholder subject for blocks that didn't carry one (inline replies)."""
    base = parent_subject or "(no subject)"
    if base.lower().startswith(("fwd:", "fw:", "vs:", "re:")):
        return base
    return f"Re: {base}"


__all__ = [
    "ExtractedMessage",
    "ParsedHeader",
    "extract_history_blocks",
    "parse_header",
]
