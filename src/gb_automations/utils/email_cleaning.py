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
    # `\*?` on both sides covers Gmail's HTML→plain conversion of Outlook's
    # bolded labels (`<b>Fra:</b>` → `*Fra:*`).
    re.compile(r"^\*?Fra:\*?\s+.+", re.IGNORECASE),
    re.compile(r"^\*?From:\*?\s+.+", re.IGNORECASE),
    re.compile(r"^\*?Sendt:\*?\s+", re.IGNORECASE),
    re.compile(r"^\*?Sent:\*?\s+", re.IGNORECASE),
    # Apple Mail / older clients sometimes use a row of underscores as a separator.
    re.compile(r"^_{5,}\s*$"),
]

# Signature sign-off phrases. Each can stand alone OR be joined with another
# via `/` or `|` to support bilingual sign-offs ("Med vennlig hilsen / Best
# Regards" is the standard form for Norwegian senders writing to international
# recipients). The trailing-anchor preserved by `_signature_pattern` keeps
# false positives down — prose can't accidentally match a full line.
_SIGNATURE_PHRASES_FULL = (
    r"Med\s+vennlig\s+hilsen",
    r"Mvh\.?",
    r"Vennlig\s+hilsen",
    r"Vh",
    r"Best\s+regards",
    r"Kind\s+regards",
    r"Regards",
    r"Sincerely",
    r"Cheers",
    r"Thanks",
    r"Thx",
    r"Hilsen",
)


def _signature_pattern(phrases: tuple[str, ...]) -> re.Pattern[str]:
    """Build a sign-off regex from `phrases` that also matches bilingual forms.

    Result accepts `<phrase>` or `<phrase> / <phrase>` or `<phrase> | <phrase>`
    on its own line (trailing comma + whitespace tolerated). Phrases are joined
    by alternation; case-insensitive.
    """
    phrase_group = "|".join(phrases)
    return re.compile(
        rf"^(?:{phrase_group})(?:\s*[/|]\s*(?:{phrase_group}))?,?\s*$",
        re.IGNORECASE,
    )


# Signature markers: cut everything from here. The two non-phrase markers
# (bare `--` and "Sent from my ..." mobile-client tag) are kept separate
# because they don't combine with bilingual partners.
_SIGNATURE_MARKERS = [
    _signature_pattern(_SIGNATURE_PHRASES_FULL),
    re.compile(r"^--\s*$"),
    re.compile(r"^Sent from my (iPhone|iPad|Android|mobile)", re.IGNORECASE),
]

# Detect-only marker for findSignatureStartLine — same phrase set as the
# cutter, just without the `--` / mobile-client variants (those are too
# easy to confuse with signature decorations sitting AFTER the real sign-off).
_SIGNATURE_START_REGEX = _signature_pattern(_SIGNATURE_PHRASES_FULL)

# Stricter set used when extracting a signature block. Drops "Cheers", "Thanks",
# "Thx", "Hilsen" — phrases ambiguous enough that body content sometimes ends
# with them ("Cheers!" mid-message). The strict set is for situations where
# we want to be SURE we found a real sign-off.
_SIGNATURE_PHRASES_STRICT = (
    r"Med\s+vennlig\s+hilsen",
    r"Mvh\.?",
    r"Vennlig\s+hilsen",
    r"Best\s+regards",
    r"Kind\s+regards",
    r"Regards",
    r"Sincerely",
)
_SIGNATURE_BLOCK_START_REGEX = _signature_pattern(_SIGNATURE_PHRASES_STRICT)

_QUOTE_PREFIX = re.compile(r"^>+\s?")

# Pattern for `[image: NAME]` markers Gmail emits in plain-text bodies for
# inline images. We strip these by default but callers can opt in to keep
# specific filenames so Goldbox can see WHERE in the body an attached image
# was referenced. Captures the filename so we can match against an allow-list.
_IMAGE_MARKER_RE = re.compile(r"\[image:\s*([^\]]+?)\s*\]", re.IGNORECASE)

# Non-image inline noise stripped unconditionally.
_INLINE_NOISE = [
    re.compile(r"<http[^>]+>"),
]


# Corporate "external sender" banners that mail servers inject into incoming
# email. They show up *inside* the message body when employees later reply or
# forward, polluting the extracted content AND sometimes hiding real reply
# boundaries that sit nearby. Stripping them before any other processing keeps
# both the Notion-displayed body and the splitter clean.
#
# Each pattern matches the WHOLE banner block (multi-line) so we can remove it
# in one substitution. Anchoring on distinctive phrases like "originated from
# outside" or "MailRisk" minimizes false positives on regular prose.
_EXTERNAL_BANNER_PATTERNS = [
    # MailRisk (Thon Eiendom and other Norwegian corporates). 4-line block,
    # always opens with `*Caution:*` (sometimes `Caution:` without asterisks)
    # and ends within a few lines that mention "MailRisk".
    re.compile(
        r"^\*?Caution:\*?[^\n]*\n"
        r"(?:[^\n]*\n){0,5}?"
        r"[^\n]*MailRisk[^\n]*\n?",
        flags=re.IGNORECASE | re.MULTILINE,
    ),
    # Microsoft Defender "first-time sender" banner. Often single-line but can
    # wrap to two. Distinctive opener `You don't often get email from`.
    re.compile(
        r"^You don'?t often get email from[^\n]*\n"
        r"(?:[^\n]*Learn why this is important[^\n]*\n?)?",
        flags=re.IGNORECASE | re.MULTILINE,
    ),
]


def strip_external_sender_banners(text: str) -> str:
    """Remove corporate 'this email is from outside' banners from a body.

    Called by clean_body but also exposed so find_reply_boundary_ranges can
    work against a pre-stripped copy when locating boundaries.
    """
    if not text:
        return text
    for pattern in _EXTERNAL_BANNER_PATTERNS:
        text = pattern.sub("", text)
    return text


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
# Optional `*` around the label handles Gmail's HTML→plain conversion of
# Outlook's bolded headers (`*Fra:*`, `*Sendt:*`, etc.). `Kopi` is Norwegian
# Outlook's Cc label — same role as Cc in the extension set.
_FORWARD_HEADER_LINE = re.compile(
    r"^\*?(From|Fra|Sent|Sendt|To|Til|Cc|Kopi|Subject|Emne|Date|Dato):\*?\s+.+",
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


def _next_nonblank_stripped(lines: list[str], start: int) -> str:
    """Look ahead from `start` for the next non-blank, quote-stripped line.

    Returns the stripped text, or empty string if end-of-list. Used by the
    boundary scanner to peek across a blank gap and decide whether the next
    non-blank line continues a forward-header block (HTML→text conversions
    of Outlook's `<p>Fra:</p><p>Sendt:</p>` paragraphs insert blank lines
    between every label).
    """
    for j in range(start, len(lines)):
        s = _QUOTE_PREFIX.sub("", lines[j]).strip()
        if s:
            return s
    return ""


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

    Boundary closing: a blank line followed by anything that isn't another
    forward-header-extension line, OR any non-extension line directly.
    The blank-gap tolerance is needed because Outlook's HTML wraps each
    header label in a `<p>` paragraph; converting that to text inserts a
    blank line between every label, breaking the old "blank line closes"
    rule.

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
                # Peek: only close if the next non-blank line ISN'T a
                # forward-header-extension. Lets us span the blank gaps
                # Outlook's HTML→text rendering inserts between `<p>Fra:</p>`
                # paragraphs.
                next_line = _next_nonblank_stripped(lines, i + 1)
                if next_line and _FORWARD_HEADER_LINE.match(next_line):
                    continue
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


def clean_body(text: str, *, keep_image_markers: set[str] | None = None) -> str:
    """Aggressively strip a Gmail body down to the sender's new content.

    `keep_image_markers`: filenames whose `[image: NAME]` markers should
    survive the strip — used to preserve in-body anchors for attachments
    that ARE being uploaded to this row. Filenames in this set are kept
    verbatim; any `[image: NAME]` marker not in the set is removed. Default
    None strips ALL markers (legacy behavior for callers that don't yet pass
    the attribution info).

    Returns empty string if the message has no original content (pure forward/quote).
    """
    if not text:
        return ""

    # Strip corporate "external sender" banners (MailRisk, Defender, …) before
    # line-by-line work so their inner punctuation (e.g. `Do *not* click`)
    # can't be confused with content markers.
    text = strip_external_sender_banners(text)

    lines = _normalize(text)

    # Step 1: cut at the start of any quoted/forwarded section.
    cut_idx = _first_match_index(lines, _REPLY_MARKERS)
    lines = lines[:cut_idx]

    # Step 2: cut at signature.
    cut_idx = _first_match_index(lines, _SIGNATURE_MARKERS)
    lines = lines[:cut_idx]

    # Step 3: strip residual quote markers and inline noise.
    keep_lower = {f.lower() for f in keep_image_markers} if keep_image_markers else set()

    def _filter_image_marker(match: re.Match[str]) -> str:
        # Filename group is captured by _IMAGE_MARKER_RE; keep the whole marker
        # iff the filename is allow-listed. Strip otherwise.
        return match.group(0) if match.group(1).strip().lower() in keep_lower else ""

    cleaned = []
    for line in lines:
        line = _QUOTE_PREFIX.sub("", line)
        line = _IMAGE_MARKER_RE.sub(_filter_image_marker, line)
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
