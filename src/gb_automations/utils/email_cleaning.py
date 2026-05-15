"""Strip a Gmail plain-text body down to just the new content.

Ports `cleanBody`, `extractSignatureBlock`, `findSignatureStartLine` from
the Apps Script `30 utils.gs`. Regex patterns kept identical for behavior parity.
Handles English + Norwegian markers (the original target audience speaks both).
"""

import re
from dataclasses import dataclass

# Reply markers: indicate the start of a quoted/forwarded section.
# We cut everything from the first match onwards.
# Reply-quotation header patterns. The shared semantic shape across email
# clients is `<some date-ish text> <skrev|wrote> <name>[<email>]:` — we match
# that broadly. The "date-ish" requirement (at least one digit in the prefix)
# is what keeps casual prose like "John wrote a long letter" from matching.
#
# Examples handled:
#   English (Gmail/Apple Mail):    "On May 13, 2026, at 2:30 PM, John <j@x> wrote:"
#   English bare:                   "On Mon, May 13, John wrote:"
#   Norwegian Gmail with kl.:       "fre. 8. mai 2026 kl. 13:55 skrev Hedda:"
#   Norwegian Gmail comma-time:     "man. 4. mai 2026, 15:43 skrev Hedda:"
#   Norwegian without time:         "Den 13. mai skrev Anne:"
#   Norwegian short:                "12. mai skrev Anne:"
_REPLY_MARKERS = [
    # English: "<anything with a digit> wrote:" — catches On-prefixed and bare.
    re.compile(r"^.*\d.*\s+wrote:\s*$", re.IGNORECASE),
    # Norwegian: "<anything with a digit> skrev <name>[:]"
    re.compile(r"^.*\d.*\s+skrev\s+.+?:?\s*$", re.IGNORECASE),
    # Norwegian "Den X skrev Y" form — covers cases without a digit when "Den"
    # is the giveaway ("Den i går skrev Anne:" type phrasings).
    re.compile(r"^Den\s+.+\s+skrev\s+.+:?\s*$", re.IGNORECASE),
    # Definitive forward-block dividers (multi-language) — also listed in
    # _DEFINITIVE_FORWARD_MARKERS below; duplicated here so they also act as
    # reply markers that clean_body's "cut at first marker" path catches them.
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Videresendt melding\s*-{3,}", re.IGNORECASE),
    # Outlook-style header blocks: From:/Fra: opens, the rest extends via
    # _FORWARD_HEADER_LINE in find_reply_boundary_ranges.
    re.compile(r"^Fra:\s+.+", re.IGNORECASE),
    re.compile(r"^From:\s+.+", re.IGNORECASE),
    re.compile(r"^Sendt:\s+", re.IGNORECASE),
    re.compile(r"^Sent:\s+", re.IGNORECASE),
    # Apple Mail / older clients sometimes use a row of underscores as a separator.
    re.compile(r"^_{5,}\s*$"),
]

# Signature markers: cut everything from here.
_SIGNATURE_MARKERS = [
    re.compile(r"^Med\s+vennlig\s+hilsen,?\s*$", re.IGNORECASE),
    re.compile(r"^Mvh\.?,?\s*$", re.IGNORECASE),
    re.compile(r"^Vennlig\s+hilsen,?\s*$", re.IGNORECASE),
    re.compile(r"^Vh,?\s*$", re.IGNORECASE),
    re.compile(r"^Best\s+regards,?\s*$", re.IGNORECASE),
    re.compile(r"^Kind\s+regards,?\s*$", re.IGNORECASE),
    re.compile(r"^Regards,?\s*$", re.IGNORECASE),
    re.compile(r"^Sincerely,?\s*$", re.IGNORECASE),
    re.compile(r"^Cheers,?\s*$", re.IGNORECASE),
    re.compile(r"^Thanks,?\s*$", re.IGNORECASE),
    re.compile(r"^Thx,?\s*$", re.IGNORECASE),
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my (iPhone|iPad|Android|mobile)", re.IGNORECASE),
    re.compile(r"^Hilsen,?\s*$", re.IGNORECASE),
]

# Detect-only marker for findSignatureStartLine (a slightly broader set).
_SIGNATURE_START_REGEX = re.compile(
    r"^(Med\s+vennlig\s+hilsen|Mvh\.?|Vennlig\s+hilsen|Vh|Best\s+regards|"
    r"Kind\s+regards|Regards|Sincerely|Cheers|Hilsen),?\s*$",
    re.IGNORECASE,
)

# Stricter markers used when extracting a signature block (won't include "Cheers"/"Hilsen").
_SIGNATURE_BLOCK_START_REGEX = re.compile(
    r"^(Med\s+vennlig\s+hilsen|Mvh\.?|Vennlig\s+hilsen|"
    r"Best\s+regards|Kind\s+regards|Regards|Sincerely),?\s*$",
    re.IGNORECASE,
)

_QUOTE_PREFIX = re.compile(r"^>+\s?")
_INLINE_NOISE = [
    re.compile(r"\[image:\s*[^\]]+\]", re.IGNORECASE),
    re.compile(r"<http[^>]+>"),
]


def _normalize(text: str) -> list[str]:
    """Normalize line endings and split into lines.

    Gmail sometimes flattens newlines into double-spaces; we restore them
    by treating any run of 2+ spaces/tabs as a newline.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]{2,}", "\n", normalized)
    return normalized.split("\n")


def _first_match_index(lines: list[str], patterns: list[re.Pattern[str]]) -> int:
    """Find the first line index where any pattern matches (after stripping quote prefix).

    Returns len(lines) if no match (caller can use as a slice end).
    """
    for i, raw in enumerate(lines):
        stripped = _QUOTE_PREFIX.sub("", raw).strip()
        if not stripped:
            continue
        for pattern in patterns:
            if pattern.match(stripped):
                return i
    return len(lines)


def count_reply_markers(text: str) -> int:
    """Count how many lines look like a reply/forward boundary.

    Kept for debugging/introspection. The sync engine uses
    `looks_like_forward_block` instead — a tighter heuristic that distinguishes
    a real forward (where header lines cluster as a block) from a normal reply
    that happens to quote one `From:` line.
    """
    if not text:
        return 0
    lines = _normalize(text)
    count = 0
    for raw in lines:
        stripped = _QUOTE_PREFIX.sub("", raw).strip()
        if not stripped:
            continue
        for pattern in _REPLY_MARKERS:
            if pattern.match(stripped):
                count += 1
                break
    return count


# Definitive forward-block headers — a single match is enough proof.
_DEFINITIVE_FORWARD_MARKERS = [
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Videresendt melding\s*-{3,}", re.IGNORECASE),
]

# Header-style lines that cluster inside a forward block. A single one of these
# can appear in a normal reply (Gmail's quoted-text rendering), so we require
# two *consecutive* non-blank lines to confirm a forward block.
_FORWARD_HEADER_LINE = re.compile(
    r"^(From|Fra|Sent|Sendt|To|Til|Cc|Subject|Emne|Date|Dato):\s+.+",
    re.IGNORECASE,
)


@dataclass
class ReplyBoundary:
    """One detected reply/forward boundary in a body.

    A boundary spans from `header_start_line` to `header_end_line` (inclusive)
    and represents either a single-line inline reply marker ("X skrev Y:") or
    a multi-line forward header block ("---- Forwarded ----" + From/Date/...).

    The block's *body* (the actual quoted message content) starts at
    `header_end_line + 1` and runs until the next boundary's `header_start_line`,
    or end-of-text for the last boundary.
    """

    header_start_line: int
    header_end_line: int
    header_text: str  # joined header lines (newline-separated), unquoted

    @property
    def label(self) -> str:
        """Short single-line label, truncated to 120 chars — handy for logging."""
        first_line = self.header_text.split("\n", 1)[0]
        return first_line[:120]


def find_reply_boundary_ranges(text: str) -> list[ReplyBoundary]:
    """Locate every reply/forward boundary in `text` as line ranges.

    Boundary opening: a line matching either a `_DEFINITIVE_FORWARD_MARKERS`
    pattern (e.g. `---- Forwarded message ----`) or a `_REPLY_MARKERS` pattern
    (e.g. `On X wrote:`, `Fra:`, `From:`).

    Boundary extension: once open, any *forward-header-style* line on a
    consecutive non-blank line continues the same boundary. This is a wider
    set than the opening patterns — it includes `Date:`, `Subject:`, `To:`,
    `Cc:`, `Emne:`, `Dato:`, `Til:` — which never OPEN a boundary (too common
    as plain prose) but absolutely belong to a boundary that's already underway.

    Boundary closing: a blank line OR any line that's neither a marker nor a
    forward-header-extension line.

    The caller slices the original text between boundaries to get block bodies.
    """
    if not text:
        return []
    lines = _normalize(text)
    out: list[ReplyBoundary] = []
    in_boundary = False
    start_line = -1
    header_lines: list[str] = []
    for i, raw in enumerate(lines):
        stripped = _QUOTE_PREFIX.sub("", raw).strip()
        if not stripped:
            if in_boundary:
                out.append(_close_boundary(start_line, i - 1, header_lines))
                in_boundary = False
                header_lines = []
            continue
        is_opener = any(p.match(stripped) for p in _DEFINITIVE_FORWARD_MARKERS) or any(
            p.match(stripped) for p in _REPLY_MARKERS
        )
        is_extension = bool(_FORWARD_HEADER_LINE.match(stripped))
        if is_opener:
            if not in_boundary:
                start_line = i
                header_lines = [stripped]
                in_boundary = True
            else:
                header_lines.append(stripped)
        elif is_extension and in_boundary:
            # Continues the current boundary, doesn't open one on its own.
            header_lines.append(stripped)
        else:
            if in_boundary:
                out.append(_close_boundary(start_line, i - 1, header_lines))
                in_boundary = False
                header_lines = []
    if in_boundary:
        out.append(_close_boundary(start_line, len(lines) - 1, header_lines))
    return out


def _close_boundary(start: int, end: int, header_lines: list[str]) -> ReplyBoundary:
    return ReplyBoundary(
        header_start_line=start,
        header_end_line=end,
        header_text="\n".join(header_lines),
    )


def find_reply_boundaries(text: str) -> list[str]:
    """Thin wrapper that returns just the labels — kept for any callers that
    don't need the line-range data. New code should use
    `find_reply_boundary_ranges` instead.
    """
    return [b.label for b in find_reply_boundary_ranges(text)]


def has_quoted_history_hint(text: str) -> bool:
    """Cheap "does this body contain ANY quoted/forwarded history?" check.

    True iff the body contains at least one reply marker (`On X wrote:`,
    `Den X skrev:`, `Fra:`, `From:`, etc.) OR a definitive forward divider
    (`---------- Forwarded message ----------` in English or Norwegian).

    Used by the sync engine to decide whether the FIRST message of a thread
    is worth running the LLM history-reconstruction on. We only call the LLM
    when there's a hint of quoted content to reconstruct.

    Cheaper than the deprecated `looks_like_forward_block` because we exit on
    the first match rather than counting or doing consecutive-line analysis.
    """
    if not text:
        return False
    for raw in _normalize(text):
        stripped = _QUOTE_PREFIX.sub("", raw).strip()
        if not stripped:
            continue
        for pattern in _DEFINITIVE_FORWARD_MARKERS:
            if pattern.match(stripped):
                return True
        for pattern in _REPLY_MARKERS:
            if pattern.match(stripped):
                return True
    return False


def looks_like_forward_block(text: str) -> bool:
    """DEPRECATED — see `has_quoted_history_hint` and the MVP-aligned history
    reconstruction model in `sync_thread._maybe_reconstruct_history`.

    Triggering rule:
      - any line matching a definitive marker ("---------- Forwarded message ----------"
        in English or Norwegian, "----- Original Message -----"), OR
      - two consecutive non-blank lines that both look like forward headers
        (`From:`, `Fra:`, `Sent:`, `Sendt:`, `To:`, `Til:`, `Cc:`,
         `Subject:`, `Emne:`, `Date:`, `Dato:`).

    Kept for tests/debug introspection only — no active code path uses it.
    Over-fires on standard reply chains where each message quotes the prior
    history (which is already its own Gmail message in the thread).
    """
    if not text:
        return False
    lines = _normalize(text)

    # Pre-strip quote prefixes once so we can scan consecutively below.
    stripped_lines = [_QUOTE_PREFIX.sub("", line).strip() for line in lines]

    # Rule 1: any definitive marker → True.
    for line in stripped_lines:
        if not line:
            continue
        for pattern in _DEFINITIVE_FORWARD_MARKERS:
            if pattern.match(line):
                return True

    # Rule 2: two consecutive non-blank lines that both look like headers.
    prev_was_header = False
    for line in stripped_lines:
        if not line:
            # Blank line resets the consecutive-streak counter.
            prev_was_header = False
            continue
        is_header = bool(_FORWARD_HEADER_LINE.match(line))
        if is_header and prev_was_header:
            return True
        prev_was_header = is_header
    return False


def clean_body(text: str) -> str:
    """Aggressively strip a Gmail body down to the sender's new content.

    Returns empty string if the message has no original content (pure forward/quote).
    """
    if not text:
        return ""

    lines = _normalize(text)

    # Step 1: cut at the start of any quoted/forwarded section.
    cut_idx = _first_match_index(lines, _REPLY_MARKERS)
    lines = lines[:cut_idx]

    # Step 2: cut at signature.
    cut_idx = _first_match_index(lines, _SIGNATURE_MARKERS)
    lines = lines[:cut_idx]

    # Step 3: strip residual quote markers and inline noise.
    cleaned = []
    for line in lines:
        line = _QUOTE_PREFIX.sub("", line)
        for pattern in _INLINE_NOISE:
            line = pattern.sub("", line)
        cleaned.append(line.rstrip())

    # Step 4: trim leading/trailing blank lines, collapse runs of blanks to one.
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    collapsed: list[str] = []
    blank_run = 0
    for line in cleaned:
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                collapsed.append("")
        else:
            blank_run = 0
            collapsed.append(line)

    return "\n".join(collapsed).strip()


def extract_signature_block(text: str) -> str:
    """Pull the signature region out of a sender's own message content.

    Stops at the first forward/reply marker so a quoted person's signature
    is never extracted as the sender's. Returns empty string if no signature
    marker is present.
    """
    if not text:
        return ""

    lines = _normalize(text)

    # Trim away anything after a forward/reply marker so we only look at the sender's content.
    cut_idx = _first_match_index(lines, _REPLY_MARKERS)
    lines = lines[:cut_idx]

    # Find the signature start within the sender's own content.
    for i, line in enumerate(lines):
        if _SIGNATURE_BLOCK_START_REGEX.match(line.strip()):
            return "\n".join(lines[i:])
    return ""


def find_signature_start_line(text: str) -> int:
    """Index of the FINAL signature marker line in `text`, or -1 if none.

    Scans bottom-up so we find the signature of the message-as-received,
    not a signature from a forwarded segment higher up. Used by attachment
    handling to detect signature-region images (e.g. logo PNGs after Mvh).
    """
    if not text:
        return -1
    lines = _normalize(text)
    for i in range(len(lines) - 1, -1, -1):
        if _SIGNATURE_START_REGEX.match(lines[i].strip()):
            return i
    return -1


def find_attachment_reference_line(text: str, attachment_name: str) -> int:
    """Line index where `[image: attachment_name]` appears in `text`, or -1.

    Used together with find_signature_start_line to detect signature-region
    images that should not be treated as real attachments.
    """
    if not text or not attachment_name:
        return -1
    lines = _normalize(text)
    escaped = re.escape(attachment_name)
    pattern = re.compile(rf"\[image:\s*{escaped}\s*\]", re.IGNORECASE)
    for i, line in enumerate(lines):
        if pattern.search(line):
            return i
    return -1
