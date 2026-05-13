"""Norwegian-friendly phone number extraction from a chunk of text.

Ports `extractPhone` and `isMobileContext` from `30 utils.gs`. Handles +47
international, Norwegian 8-digit local, and prefers mobile numbers when context
markers ("Mob", "Mobil", "M:") appear nearby.
"""

import re

# International with optional country code, e.g. "+47 999 88 777", "(+1) 415-555-1234"
_INTL_RE = re.compile(r"\(?\+\s*\d{1,3}\)?[\s\-.]*\d[\d\s\-.]{6,14}\d")

# Norwegian 8-digit local, various groupings. Captures up to 11 digits then we
# verify the digit count is exactly 8 in code.
_LOCAL_RE = re.compile(
    r"(?:^|[^\d+])(\d{2,3}[\s\-.]?\d{2,3}[\s\-.]?\d{2,3}[\s\-.]?\d{0,2})(?=$|[^\d])"
)

_MOBILE_CONTEXT_RE = re.compile(r"(mob|mobil|m[:.]\s*$|cell)", re.IGNORECASE)


def _is_mobile_context(text: str, match_index: int) -> bool:
    """True if the 15 characters immediately before `match_index` look like a mobile label."""
    context = text[max(0, match_index - 15) : match_index].lower()
    return bool(_MOBILE_CONTEXT_RE.search(context))


def extract_phone(text: str) -> str | None:
    """Pull a single phone number out of `text`. Prefers mobile when both are present.

    Returns None if no plausible number is found. Returned string has runs of
    spaces/dashes/dots normalized to single spaces.
    """
    if not text:
        return None

    candidates: list[tuple[str, int, bool]] = []  # (value, index, is_mobile)

    for m in _INTL_RE.finditer(text):
        candidates.append((m.group(0), m.start(), _is_mobile_context(text, m.start())))

    for m in _LOCAL_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if len(digits) == 8:
            candidates.append((m.group(1).strip(), m.start(), _is_mobile_context(text, m.start())))

    if not candidates:
        return None

    chosen = next((c for c in candidates if c[2]), candidates[0])  # mobile, else first
    return re.sub(r"[\s\-.]+", " ", chosen[0]).strip()
