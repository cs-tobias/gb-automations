"""Strip a Gmail plain-text body down to just the new content.

Ports `cleanBody`, `extractSignatureBlock`, `findSignatureStartLine` from
the Apps Script `30 utils.gs`. Regex patterns kept identical for behavior parity.
Handles English + Norwegian markers (the original target audience speaks both).
"""

import re

# Reply markers: indicate the start of a quoted/forwarded section.
# We cut everything from the first match onwards.
_REPLY_MARKERS = [
    re.compile(r"^On .+ wrote:\s*$", re.IGNORECASE),
    # Norwegian "skrev" with time prefix: "12. mai kl. 14:30, skrev X:"
    re.compile(r"^.{3,}\s+kl\.\s+\d{1,2}[:.]\d{2},?\s+skrev\s+.+:?\s*$", re.IGNORECASE),
    re.compile(r"^Den\s+.+\s+skrev\s+.+:?\s*$", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Forwarded message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^-{3,}\s*Original Message\s*-{3,}", re.IGNORECASE),
    re.compile(r"^Fra:\s+.+", re.IGNORECASE),  # Norwegian "From:" header in forwards
    re.compile(r"^From:\s+.+", re.IGNORECASE),
    re.compile(r"^Sendt:\s+", re.IGNORECASE),
    re.compile(r"^Sent:\s+", re.IGNORECASE),
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
