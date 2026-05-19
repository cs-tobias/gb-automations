"""Parse "Name <email@host>" header values + classify internal vs external addresses.

Ports the participant-parsing helpers from `30 utils.gs`. Internal-domain detection
is configured via the comma-separated INTERNAL_EMAILS_OR_DOMAINS env var (loaded once
per call — small, no need to cache).
"""

import os
import re
from dataclasses import dataclass

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NAME_BEFORE_EMAIL_RE = re.compile(r"^([^<]+?)\s*<[^>]+>")
_EMAIL_IN_BRACKETS_RE = re.compile(r"<([^>]+)>")
_GENERIC_LOCAL_NAMES = {"post", "info", "contact", "hello", "admin", "noreply", "no-reply"}


@dataclass
class Participant:
    name: str | None
    email: str  # always lowercased


def extract_email(from_field: str) -> str:
    """Pull `email@host` out of a raw "Name <email@host>" header. Returns the trimmed
    field if no angle brackets are present.
    """
    match = _EMAIL_IN_BRACKETS_RE.search(from_field)
    return match.group(1) if match else from_field.strip()


def extract_name(from_field: str) -> str:
    """Pull the display name out of a "Name <email@host>" header.

    Falls back to the email's local-part if no display name is present.
    """
    match = re.match(r"^([^<]+?)\s*<", from_field)
    if match:
        return re.sub(r"^[\"']|[\"']$", "", match.group(1)).strip()
    return from_field.split("@")[0]


def parse_participant(raw: str) -> Participant | None:
    """Parse a "Name <email@host>" string into name + email parts.

    Returns None if no email can be found. Strict about names: returns None for the name
    when the display name is missing, equals the email/local-part, or is a generic alias
    like "post" or "info" (these are noise, not real names).
    """
    if not raw or not raw.strip():
        return None

    email_match = _EMAIL_RE.search(raw)
    if not email_match:
        return None
    email = email_match.group(0).lower()

    name = ""
    name_match = _NAME_BEFORE_EMAIL_RE.match(raw)
    if name_match:
        name = name_match.group(1).strip().strip("\"'").strip()
        name = re.sub(r"\s+", " ", name)

    lower_name = name.lower()
    local_part = email.split("@")[0]
    looks_generic = lower_name in _GENERIC_LOCAL_NAMES
    is_just_the_email = lower_name == email or lower_name == local_part

    if not name or looks_generic or is_just_the_email:
        return Participant(name=None, email=email)
    return Participant(name=name, email=email)


def is_internal(email: str) -> bool:
    """Check if an email is in the internal domain/address list.

    Reads `INTERNAL_EMAILS_OR_DOMAINS` env var: comma-separated list of either
    full addresses (exact match) or bare domains (suffix match on `@domain`).
    Example: `tobias@goldbox.no, post@goldbox.no, goldbox.no`

    Falls back to `WORKSPACE_DOMAIN` (the Google Workspace domain already
    configured for Gmail DWD) when `INTERNAL_EMAILS_OR_DOMAINS` is empty. So
    a fresh deployment that just sets `WORKSPACE_DOMAIN=goldbox.no` gets
    "internal = anyone at goldbox.no" for free, without setting two env vars
    that mean the same thing.
    """
    raw = os.environ.get("INTERNAL_EMAILS_OR_DOMAINS", "").strip()
    if not raw:
        raw = os.environ.get("WORKSPACE_DOMAIN", "").strip()
    entries = [s.strip().lower() for s in raw.split(",") if s.strip()]
    e = email.lower()
    for entry in entries:
        if "@" in entry:
            if e == entry:
                return True
        elif e.endswith("@" + entry):
            return True
    return False


# Free-mail / consumer-mailbox providers. A `@gmail.com` address is a person,
# not a representative of "Gmail the company" — and the same goes for every
# other consumer provider. We use this to skip company-row creation for these
# domains entirely; the contact still exists, just with no Company relation.
#
# Extend this list rather than guessing in code at the call site. Coverage
# target: the long tail of Norwegian + international consumer providers Goldbox
# is likely to receive briefs from. Add new entries here when you spot a real
# "person, not company" row landing in the Companies DB.
_FREE_MAIL_DOMAINS = frozenset({
    # Google
    "gmail.com",
    "googlemail.com",
    # Microsoft
    "outlook.com",
    "outlook.no",
    "hotmail.com",
    "hotmail.no",
    "hotmail.co.uk",
    "live.com",
    "live.no",
    "msn.com",
    # Apple
    "icloud.com",
    "me.com",
    "mac.com",
    # Yahoo / AOL
    "yahoo.com",
    "yahoo.no",
    "yahoo.co.uk",
    "ymail.com",
    "aol.com",
    # ProtonMail
    "proton.me",
    "protonmail.com",
    "pm.me",
    # GMX
    "gmx.com",
    "gmx.de",
    "gmx.net",
    # Norwegian ISPs / consumer
    "online.no",
    "broadpark.no",
    "getmail.no",
    "start.no",
    "c2i.net",
})


def is_free_mail_domain(domain: str) -> bool:
    """True for consumer/free-mail providers (gmail.com, outlook.com, …).

    Used to suppress company-row creation: an `@gmail.com` address is a
    person, not a representative of a "Gmail" company. The contact is still
    created — only the Company relation is left empty.

    Empty/invalid domains return False (caller already filters those out
    before this check matters).
    """
    return domain.lower() in _FREE_MAIL_DOMAINS


def company_from_domain(email: str) -> str:
    """Best-effort company name from the email domain.

    "ht@metropolis.no" → "Metropolis"
    "person@sub.company.com" → "Company"
    """
    domain = email.split("@", 1)[1] if "@" in email else ""
    parts = domain.split(".")
    if not parts or not parts[0]:
        return ""
    main = parts[-2] if len(parts) >= 2 else parts[0]
    return main.capitalize()


def find_sender_email(from_field: str) -> str:
    """Lowercase email address from a "Name <email>" or bare-email header value."""
    m = _EMAIL_IN_BRACKETS_RE.search(from_field)
    return (m.group(1) if m else from_field).strip().lower()


def strict_email_or_empty(field: str) -> str:
    """Return the email from a 'Name <email>' or bare-email field, or '' if there isn't one.

    Unlike find_sender_email(), this does NOT fall back to returning the bare
    string when no email pattern is detected. Used when the field comes from
    an LLM that may have returned just a display name ("Petter Burhol") — in
    that case we want to leave the Notion `email`-typed property unset rather
    than write the name into it.
    """
    m = _EMAIL_RE.search(field)
    return m.group(0).lower() if m else ""
