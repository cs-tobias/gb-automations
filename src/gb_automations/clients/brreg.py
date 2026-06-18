"""Open Norwegian Business Registry (Enhetsregisteret) client.

Brreg's REST API is unauthenticated, free, and returns the authoritative
legal name + metadata for any registered Norwegian entity. The Fiken
engine uses it for two enrichment paths during customer resolution:

  1. Orgnr present in Notion → `get_enhet(orgnr)` returns the official
     `navn` so the auto-created Fiken contact carries Brreg's legal
     name (instead of whatever shortcut the operator typed into the
     Fakturamottaker title).
  2. Orgnr blank but the Project has a Kunder relation with a name →
     `search_enheter(name)` + `pick_exact_match` find the single clean
     match by suffix-aware rules. Result: we can recover the missing
     Orgnr from a casual customer name like "Entur" without prompting.

Brreg is best-effort everywhere: timeouts, 5xx, 404, and no-match all
return None / empty list so the caller falls through to today's
behavior. Never raises (catches at the boundary), never blocks a draft.

Brreg's quirks worth remembering (verified empirically against
data.brreg.no):
  - Base URL: `https://data.brreg.no/enhetsregisteret/api`. NOT
    `/brreg.no/…` — that's the public website.
  - `GET /enheter/{orgnr}` returns the single entity dict on 200, 404
    when the Orgnr is unknown.
  - `GET /enheter?navn=X&size=N` returns a HAL-style envelope:
    `{"_embedded": {"enheter": [...]}, "page": {...}}`. Empty result
    arrives as `{"page": {...}}` with no `_embedded` key — handle the
    missing key, don't index it.
  - The `navn` field is always uppercase in Brreg's data
    (`ENTUR AS`, not `Entur AS`); the match helper here lowercases
    both sides for comparison.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


BRREG_API_BASE = "https://data.brreg.no/enhetsregisteret/api"
# Brreg is fast (<200ms typical) and we hit it once per draft creation;
# a short timeout keeps a draft from waiting on a degraded registry.
_HTTP_TIMEOUT = 10.0


# Norwegian legal entity suffixes accepted as a clean trailing token in
# `pick_exact_match`. Ordered roughly by frequency; the match function
# tries all of them. Add new forms here as they show up in production
# Brreg results.
NORWEGIAN_ENTITY_SUFFIXES: tuple[str, ...] = (
    "AS",       # Aksjeselskap (private limited)
    "ASA",      # Allmennaksjeselskap (public limited)
    "ANS",      # Ansvarlig selskap
    "DA",       # Delt ansvar
    "ENK",      # Enkeltpersonforetak
    "SA",       # Samvirkeforetak
    "BA",       # Begrenset ansvar
    "NUF",      # Norskregistrert utenlandsk foretak
    "AL",       # Andelslag
    "BBL",      # Boligbyggelag
    "BL",       # Borettslag
    "KS",       # Kommandittselskap
    "SE",       # Europeisk selskap
    "FKF",      # Fylkeskommunalt foretak
)


def _normalize_orgnr(raw: str | None) -> str:
    """Digits-only normalization. Shared shape with the Fiken engine's
    `_normalize_orgnr` but kept here too so this module is self-contained
    (no engine import).
    """
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


async def get_enhet(orgnr: str) -> dict[str, Any] | None:
    """Look up a single registered entity by organization number.

    Returns the Brreg entity dict on success, None on 404 or any error.
    Caller is expected to read at least `navn` (the official legal
    name); the dict also carries `organisasjonsnummer`, `organisasjonsform`,
    `forretningsadresse`, etc. for richer enrichment if needed later.
    """
    cleaned = _normalize_orgnr(orgnr)
    if not cleaned:
        return None
    try:
        async with httpx.AsyncClient(
            base_url=BRREG_API_BASE, timeout=_HTTP_TIMEOUT
        ) as client:
            response = await client.get(f"/enheter/{cleaned}")
    except Exception as err:  # noqa: BLE001
        logger.warning("brreg get_enhet(%s) failed: %s", cleaned, err)
        return None

    if response.status_code == 404:
        return None
    if response.status_code != 200:
        logger.warning(
            "brreg get_enhet(%s): unexpected status %d (%s)",
            cleaned,
            response.status_code,
            response.text[:200],
        )
        return None
    try:
        data = response.json()
    except Exception as err:  # noqa: BLE001
        logger.warning("brreg get_enhet(%s): JSON parse failed: %s", cleaned, err)
        return None
    if not isinstance(data, dict):
        return None
    return data


async def search_enheter(
    name: str, *, size: int = 20
) -> list[dict[str, Any]]:
    """Search registered entities by name.

    Returns the `_embedded.enheter` list (or empty list on miss / error).
    `size` caps the result set; Brreg's default is 20 and the operator
    only needs enough to find a clean suffix-aware match anyway.

    Empty list on no match — Brreg returns `{"page": {...}}` without
    `_embedded` in that case; we handle the missing key explicitly.
    """
    name = (name or "").strip()
    if not name:
        return []
    try:
        async with httpx.AsyncClient(
            base_url=BRREG_API_BASE, timeout=_HTTP_TIMEOUT
        ) as client:
            response = await client.get(
                "/enheter", params={"navn": name, "size": size}
            )
    except Exception as err:  # noqa: BLE001
        logger.warning("brreg search_enheter(%r) failed: %s", name, err)
        return []

    if response.status_code != 200:
        logger.warning(
            "brreg search_enheter(%r): unexpected status %d (%s)",
            name,
            response.status_code,
            response.text[:200],
        )
        return []
    try:
        data = response.json()
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "brreg search_enheter(%r): JSON parse failed: %s", name, err
        )
        return []
    if not isinstance(data, dict):
        return []
    embedded = data.get("_embedded") or {}
    enheter = embedded.get("enheter") if isinstance(embedded, dict) else None
    return enheter if isinstance(enheter, list) else []


def pick_exact_match(
    query: str, results: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """Return the single clean match from `search_enheter` results.

    Rules (all case-insensitive, trimmed):
      - A result is a "clean match" if its `navn` equals `query` exactly,
        OR equals `query + " " + suffix` for any suffix in
        NORWEGIAN_ENTITY_SUFFIXES.
      - Return the single match. If zero or 2+ results match cleanly,
        return None — the caller then falls back to the placeholder
        path rather than guess which company the operator meant.

    Examples:
      - 'Entur' against ['ENTUR AS', 'ENTURA HOLDING AS',
        'ENTUR LANDSFORENING'] → returns the ENTUR AS dict.
      - 'Entur' against ['ENTURA AS', 'ENTURA EIENDOM AS'] → None
        (no result has navn 'Entur' or 'Entur <suffix>').
      - 'Equinor ASA' against ['EQUINOR ASA'] → returns the EQUINOR
        ASA dict (exact equals).
      - 'Voss' against 20 results none of which match 'Voss <suffix>'
        → None.
    """
    q = (query or "").strip().lower()
    if not q or not results:
        return None
    accepted: list[dict[str, Any]] = []
    candidate_names = {q}
    for suffix in NORWEGIAN_ENTITY_SUFFIXES:
        candidate_names.add(f"{q} {suffix.lower()}")
    for entry in results:
        if not isinstance(entry, dict):
            continue
        navn = (entry.get("navn") or "").strip().lower()
        if navn and navn in candidate_names:
            accepted.append(entry)
    if len(accepted) == 1:
        return accepted[0]
    return None


__all__ = [
    "BRREG_API_BASE",
    "NORWEGIAN_ENTITY_SUFFIXES",
    "get_enhet",
    "search_enheter",
    "pick_exact_match",
]
