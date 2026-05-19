"""Pull structured fields (title, address, company) out of an email signature.

Input is typically the output of `extract_signature_block()` in
`utils/email_cleaning.py`, but the parser also tolerates a raw tail of body
text (Petter's signature in the wild has no Mvh/sign-off line, so the
signature extractor returns nothing — we still want title/address/company
from those messages, and `_upsert_thread_contacts` will fall back to passing
the message body tail directly).

No LLM. The structure of a Norwegian business signature is consistent enough
that line-position + format heuristics work:

    [Med vennlig hilsen ...]   ← optional sign-off (already stripped sometimes)
    <Person Name>              ← matches sender name
    <Title>                    ← short text line, no digits/@/url
    <Phone(s)>                 ← lines containing digits and/or '+'
    <Email>                    ← line containing '@'
    <Company>                  ← short capitalized line, no digits/@/url
    <Street, postal city>      ← contains 4-digit postal code
    <URLs>                     ← .no/.com lines we ignore
"""

import re
from dataclasses import dataclass


_PHONE_HINT_RE = re.compile(r"[\d+()]")
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_URL_RE = re.compile(r"^(https?://|www\.)|\.(no|com|org|net|io|co)(/|$)", re.IGNORECASE)
# Norwegian postal codes are 4 digits followed by a city name. The space + a
# capital letter (incl. ÆØÅ) is what distinguishes "0184 Oslo" from a random
# digit run like "tlf 12345".
_NORWEGIAN_POSTAL_RE = re.compile(r"\b\d{4}\s+[A-ZÆØÅ][A-Za-zÆØÅæøå\- ]+")
# Sign-off lines we always skip even if extract_signature_block didn't.
_SIGNOFF_LINE_RE = re.compile(
    r"^(med\s+vennlig\s+hilsen|mvh\.?|vennlig\s+hilsen|vh|best\s+regards?|"
    r"kind\s+regards?|regards|sincerely|cheers|thanks|thx|hilsen)\b",
    re.IGNORECASE,
)
# Title-ish whitelist: many Norwegian role suffixes contain letters only and
# sit < 60 chars. We don't enforce these — they just help when scoring.

_MAX_TITLE_LEN = 60
_MAX_COMPANY_LEN = 60


@dataclass(frozen=True)
class SignatureFields:
    title: str | None
    address: str | None
    company: str | None


def parse_signature(signature_block: str, sender_name: str | None = None) -> SignatureFields:
    """Extract title / address / company from a signature region.

    `sender_name` is used to locate the anchor line ("the line that contains
    the sender's name"); the title line sits immediately below it. When the
    sender name isn't available or doesn't appear in the block, we fall back
    to the line just above the first phone/email line.
    """
    if not signature_block:
        return SignatureFields(None, None, None)

    lines = _clean_lines(signature_block)
    if not lines:
        return SignatureFields(None, None, None)

    name_idx = _find_name_index(lines, sender_name)
    title = _extract_title(lines, name_idx)
    address = _extract_address(lines)
    company = _extract_company(lines, name_idx=name_idx, title=title, address=address)

    return SignatureFields(title=title, address=address, company=company)


def _clean_lines(block: str) -> list[str]:
    """Split, strip, drop blanks + sign-off lines."""
    out: list[str] = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _SIGNOFF_LINE_RE.match(line):
            continue
        out.append(line)
    return out


def _is_phone_line(line: str) -> bool:
    """A line dominated by digits and phone punctuation."""
    digits = sum(c.isdigit() for c in line)
    if digits < 6:
        return False
    # Reject postal-code-only matches (4-digit + city) — those are addresses.
    if _NORWEGIAN_POSTAL_RE.search(line):
        return False
    non_phone_chars = sum(1 for c in line if c.isalpha() and c not in "MmTtFfPp")
    return non_phone_chars <= 4 or bool(_PHONE_HINT_RE.search(line))


def _is_email_line(line: str) -> bool:
    return bool(_EMAIL_RE.search(line))


def _is_url_line(line: str) -> bool:
    # Reject anything that looks like a URL but is also an email (already handled).
    if _is_email_line(line):
        return False
    return bool(_URL_RE.search(line))


def _is_address_line(line: str) -> bool:
    return bool(_NORWEGIAN_POSTAL_RE.search(line))


def _find_name_index(lines: list[str], sender_name: str | None) -> int:
    """Index of the line matching `sender_name`, or -1 if not found.

    Match is case-insensitive substring on either the full name or any token
    of it (covers "Ingeborg Kvamme Skar" appearing as "Ingeborg Skar" or vice
    versa in the signature line).
    """
    if not sender_name:
        return -1
    needle = sender_name.strip().lower()
    if not needle:
        return -1
    tokens = [t for t in needle.split() if len(t) >= 3]
    for i, line in enumerate(lines):
        low = line.lower()
        if needle in low:
            return i
        # Require at least two token hits so a first-name-only collision
        # ("Petter sent a message") doesn't anchor us in the wrong place.
        if len(tokens) >= 2 and sum(1 for t in tokens if t in low) >= 2:
            return i
    return -1


def _extract_title(lines: list[str], name_idx: int) -> str | None:
    """The line right after the name, if it's short text (no digits/@/url)."""
    if name_idx < 0:
        # Fallback: the line just above the first phone/email line.
        for i, line in enumerate(lines):
            if _is_phone_line(line) or _is_email_line(line):
                if i > 0:
                    candidate = lines[i - 1]
                    if _looks_like_title(candidate):
                        return candidate
                break
        return None

    for j in range(name_idx + 1, len(lines)):
        candidate = lines[j]
        if _looks_like_title(candidate):
            return candidate
        # First non-title line below the name ends the search — don't skip
        # over a phone line hoping to find a title further down.
        return None
    return None


def _looks_like_title(line: str) -> bool:
    if len(line) > _MAX_TITLE_LEN:
        return False
    if _is_phone_line(line) or _is_email_line(line) or _is_url_line(line):
        return False
    if _is_address_line(line):
        return False
    # Reject "all caps short codes" like "AS" which are company suffixes.
    if len(line) <= 3 and line.isupper():
        return False
    return any(c.isalpha() for c in line)


def _extract_address(lines: list[str]) -> str | None:
    """Find the postal-code line; prepend the previous line if it looks like a street."""
    for i, line in enumerate(lines):
        if _is_address_line(line):
            if i > 0:
                prev = lines[i - 1]
                if _looks_like_street(prev):
                    return f"{prev}, {line}"
            return line
    return None


def _looks_like_street(line: str) -> bool:
    """Street line: short, contains a digit (house number), no @ / no URL."""
    if len(line) > 80:
        return False
    if _is_email_line(line) or _is_url_line(line):
        return False
    has_digit = any(c.isdigit() for c in line)
    has_alpha = any(c.isalpha() for c in line)
    return has_digit and has_alpha


def _extract_company(
    lines: list[str],
    *,
    name_idx: int,
    title: str | None,
    address: str | None,
) -> str | None:
    """Find a company-name line.

    Strategy: scan lines, skipping name/title/phone/email/url/address lines.
    The first remaining short text line wins. Capitalized first letter preferred
    but not required (some companies render lowercase). When there's no
    address, the company is often the last non-noise line of the signature
    (e.g. "Petter Burhol / Daglig leder / +47.../ petter@goldbox.no /
    www.goldbox.no" — no explicit company line; we then fall back to the URL
    stem, e.g. "goldbox").
    """
    skip_lines = set()
    if name_idx >= 0:
        skip_lines.add(name_idx)

    for i, line in enumerate(lines):
        if i in skip_lines:
            continue
        if title and line == title:
            continue
        if address and (line in address or (", " in address and line == address.split(", ", 1)[0])):
            continue
        if _is_phone_line(line) or _is_email_line(line) or _is_url_line(line):
            continue
        if _is_address_line(line):
            continue
        if len(line) > _MAX_COMPANY_LEN:
            continue
        if not any(c.isalpha() for c in line):
            continue
        return line

    # Fallback: derive from the first URL line (e.g. "www.goldbox.no" → "Goldbox").
    for line in lines:
        if _is_url_line(line):
            stem = _company_from_url_line(line)
            if stem:
                return stem
    return None


def _company_from_url_line(line: str) -> str | None:
    # Strip protocol + www, take the first label.
    s = re.sub(r"^(https?://)?(www\.)?", "", line.strip(), flags=re.IGNORECASE)
    label = s.split("/", 1)[0].split(".", 1)[0]
    label = label.strip()
    return label.capitalize() if label else None
