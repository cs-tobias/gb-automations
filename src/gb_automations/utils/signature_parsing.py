"""Signature field extraction: two roles in one module.

1. `clean_*_line` — turn a single VERBATIM line (the LLM's locator output, see
   `clients/llm.py` `classify_signature`) into a clean value. The LLM is good
   at picking *which* line holds the title / phone / address even in odd
   formats; these helpers do the deterministic cleanup (strip the "m:"/"a:"
   label, drop a trailing company off the title, etc.). This is the primary
   path.

2. `parse_signature` — the regex-only BACKSTOP used when the LLM is down or
   returns nothing. It pulls title/address out of a whole signature region by
   line-position heuristics. Input is typically `extract_signature_block()`
   from `utils/email_cleaning.py`, but it also tolerates a raw body tail.

Company name is intentionally NOT derived here: the "first non-contact text
line is the company" heuristic grabbed arbitrary body sentences ("Does this
mail get added?") and bare first names ("Tobias"). Company names come solely
from the email-domain stem (`company_from_domain`) — which `clean_title_line`
also uses to strip a company suffix off the title line.

The backstop assumes a roughly canonical Norwegian business signature:

    [Med vennlig hilsen ...]   ← optional sign-off (already stripped sometimes)
    <Person Name>              ← matches sender name
    <Title>                    ← short text line, no digits/@/url
    <Phone(s)>                 ← lines containing digits and/or '+'
    <Email>                    ← line containing '@'
    <Street, postal city>      ← 4-digit postal code OR a known Norwegian city
    <URLs>                     ← .no/.com lines we ignore
"""

import re
from dataclasses import dataclass

from gb_automations.utils.phone import extract_phone

_PHONE_HINT_RE = re.compile(r"[\d+()]")
_EMAIL_RE = re.compile(r"\S+@\S+\.\S+")
_URL_RE = re.compile(r"^(https?://|www\.)|\.(no|com|org|net|io|co)(/|$)", re.IGNORECASE)
# Norwegian postal codes are 4 digits followed by a city name. The space + a
# capital letter (incl. ÆØÅ) is what distinguishes "0184 Oslo" from a random
# digit run like "tlf 12345".
_NORWEGIAN_POSTAL_RE = re.compile(r"\b\d{4}\s+[A-ZÆØÅ][A-Za-zÆØÅæøå\- ]+")
# Address fallback for the regex backstop: a line naming a known Norwegian city
# is treated as an address even when the postal-code regex misses (e.g. the
# pipe-separated "NO0258 Oslo" form where "NO" prefixes the digits and breaks
# \b\d{4}). Lowercased; matched on whole-word tokens. Not exhaustive — the
# largest towns plus the ones we've actually seen in signatures.
_NORWEGIAN_CITIES = frozenset(
    {
        "oslo", "bergen", "trondheim", "stavanger", "drammen", "fredrikstad",
        "kristiansand", "sandnes", "tromsø", "sarpsborg", "skien", "ålesund",
        "sandefjord", "haugesund", "tønsberg", "moss", "porsgrunn", "bodø",
        "arendal", "hamar", "ytrebygda", "larvik", "halden", "harstad",
        "lillehammer", "molde", "horten", "gjøvik", "askøy", "kongsberg",
        "kristiansund", "rana", "mo i rana", "jessheim", "narvik", "ski",
        "elverum", "leirvik", "nesoddtangen", "vennesla", "førde", "alta",
        "kongsvinger", "lillestrøm", "drøbak", "grimstad", "bryne", "kvinnherad",
        "stjørdal", "steinkjer", "namsos", "levanger", "verdal", "egersund",
        "mandal", "notodden", "kragerø", "risør", "florø", "voss", "odda",
        "lyngdal", "farsund", "flekkefjord", "ås", "vestby", "råde", "rygge",
        "askim", "mysen", "spydeberg", "hokksund", "vikersund", "hønefoss",
        "jaren", "raufoss", "brumunddal", "moelv", "tynset", "røros", "oppdal",
        "orkanger", "brekstad", "melhus", "malvik", "stjordal", "sortland",
        "svolvær", "leknes", "fauske", "mosjøen", "brønnøysund", "sandnessjøen",
        "finnsnes", "bardufoss", "kirkenes", "vadsø", "hammerfest", "honningsvåg",
        "kolbotn", "sandvika", "asker", "lysaker", "skøyen", "majorstuen",
        "stabekk", "fornebu", "billingstad", "slependen", "heggedal",
        "nydalen", "økern",
    }
)
# Sign-off lines we always skip even if extract_signature_block didn't.
_SIGNOFF_LINE_RE = re.compile(
    r"^(med\s+vennlig\s+hilsen|mvh\.?|vennlig\s+hilsen|vh|best\s+regards?|"
    r"kind\s+regards?|regards|sincerely|cheers|thanks|thx|hilsen)\b",
    re.IGNORECASE,
)
# Title-ish whitelist: many Norwegian role suffixes contain letters only and
# sit < 60 chars. We don't enforce these — they just help when scoring.

_MAX_TITLE_LEN = 60

# Leading contact labels on a signature line: "a:", "Tlf:", "m:", "Mob.:",
# "e:", "E-post:" etc. We strip these off a located line before storing the
# value. Tolerates ':' or '.' as the separator.
_LEADING_LABEL_RE = re.compile(
    r"^\s*(a|adr|adresse|address|t|tlf|tel|telefon|m|mob|mobil|p|ph|phone"
    r"|e|email|e-post|epost)\s*[:.]\s*",
    re.IGNORECASE,
)
# Title lines often end with a company segment after a "|" or "/" separator
# ("Daglig leder| NIMREM", "Senterleder / Goldbox"). We cut a trailing segment
# when it matches the known company; the company itself comes from the domain.
_TITLE_SPLIT_RE = re.compile(r"\s*[|/]\s*")


@dataclass(frozen=True)
class SignatureFields:
    title: str | None
    address: str | None


def _strip_leading_label(line: str) -> str:
    return _LEADING_LABEL_RE.sub("", line, count=1).strip()


def clean_phone_line(line: str | None) -> str | None:
    """Extract a normalized phone number from a single located line.

    Delegates to `extract_phone`, which already strips "m:"/"Mob.:" labels and
    normalizes separators. Returns None when the line holds no usable number.
    """
    if not line:
        return None
    return extract_phone(line)


def clean_title_line(
    line: str | None,
    known_company: str | None = None,
    sender_name: str | None = None,
) -> str | None:
    """Strip a leading label and a trailing company segment from a title line.

    `Daglig leder| NIMREM` with known_company "Nimrem" → `Daglig leder`.
    A trailing segment is only cut when it matches `known_company`
    (case-insensitive), so genuine dual roles like `Partner / Daglig leder`
    survive when the company doesn't appear.

    Validation: the LLM sometimes points at the wrong line when a sender has no
    title (it returns the NAME line) or at a URL/email. Reject those — a title
    is never the sender's own name, a website, an email, or a phone number.
    Returns None when the line is empty or fails validation.
    """
    if not line:
        return None
    cleaned = _strip_leading_label(line)
    if known_company:
        segments = _TITLE_SPLIT_RE.split(cleaned)
        if len(segments) > 1 and segments[-1].strip().lower() == known_company.strip().lower():
            cleaned = _TITLE_SPLIT_RE.split(cleaned)[:-1]
            cleaned = " / ".join(s.strip() for s in cleaned).strip()
    if not cleaned:
        return None
    if _is_url_line(cleaned) or _is_email_line(cleaned) or _is_phone_line(cleaned):
        return None
    if sender_name and cleaned.strip().lower() == sender_name.strip().lower():
        return None
    return cleaned


def parse_signature(signature_block: str, sender_name: str | None = None) -> SignatureFields:
    """Extract title / address from a signature region.

    `sender_name` is used to locate the anchor line ("the line that contains
    the sender's name"); the title line sits immediately below it. When the
    sender name isn't available or doesn't appear in the block, we fall back
    to the line just above the first phone/email line.
    """
    if not signature_block:
        return SignatureFields(None, None)

    lines = _clean_lines(signature_block)
    if not lines:
        return SignatureFields(None, None)

    name_idx = _find_name_index(lines, sender_name)
    title = _extract_title(lines, name_idx)
    address = _extract_address(lines)

    return SignatureFields(title=title, address=address)


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
    if _NORWEGIAN_POSTAL_RE.search(line):
        return True
    # Fallback: a known Norwegian city as a whole-word token. Catches forms the
    # postal regex misses, e.g. "NO0258 Oslo" or pipe-separated address lines.
    tokens = re.split(r"[^A-Za-zÆØÅæøå]+", line.lower())
    return any(t in _NORWEGIAN_CITIES for t in tokens if t)


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
    """Find the postal-code/city line; prepend the previous line if it's a street.

    A leading "a:"/"adr:" label is stripped off the result (some senders label
    the address line, e.g. "a: Parkveien 37 | NO0258 Oslo | Norway").
    """
    for i, line in enumerate(lines):
        if _is_address_line(line):
            if i > 0:
                prev = lines[i - 1]
                if _looks_like_street(prev):
                    return f"{_strip_leading_label(prev)}, {_strip_leading_label(line)}"
            return _strip_leading_label(line)
    return None


def _looks_like_street(line: str) -> bool:
    """Street line: short, contains a digit (house number), no @ / no URL."""
    if len(line) > 80:
        return False
    if _is_email_line(line) or _is_url_line(line):
        return False
    # A phone line ("Mob.: +47 90178028") has a digit + letters but is never a
    # street — without this we'd prepend it to the postal line.
    if _is_phone_line(line):
        return False
    has_digit = any(c.isdigit() for c in line)
    has_alpha = any(c.isalpha() for c in line)
    return has_digit and has_alpha
