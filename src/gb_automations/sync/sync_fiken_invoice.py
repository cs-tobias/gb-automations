"""Notion-button → Fiken-draft creation engine (Phase B, v2).

Worker entrypoint for a `send_faktura` task. The single per-project
"Send faktura" Notion button enqueues one of these; the engine reads
the project's `Faktura status` to decide the mode and acts on every
Oppgave under the project that isn't excluded.

Algorithm:

    1. Read the live Project page. Map `Faktura status` to an
       invoice_type:
         - "Til oppstartsfaktura"   → "oppstart"
         - "Til avslutningsfaktura" → "slutt"
         - anything else            → skipped (re-clicking after the
                                       run is finished is a no-op).
    2. Read all Oppgaver under the project. A row is eligible iff:
         - Type is a recognized discipline (Klargjøre modell /
           Korreksjonsrunde / blank are skipped — internal tasks).
         - `Fakturert status` is NOT in {"Fakturert", "Utgår"}.
         - For oppstart: `Fakturert status == "Ikke fakturert"`.
         - For slutt: `Fakturert status ∈ {"Ikke fakturert",
           "Fakturert 50%"}`.
    3. Per row, compute the NOK amount to bill on this run. `Pris` is
       the current agreed full price and is MUTABLE — operators can
       renegotiate between oppstart and slutt and the slutt run picks
       up the new value. `Rabatt` is a percent (0–100) subtracted before
       the split.
         gross     = Pris × (1 − Rabatt / 100)
         already   = Fakturert beløp   (the running total Notion holds)
         remaining = gross − already
         oppstart  → to_bill = round(gross × 0.5)     (50% of fresh gross)
         slutt     → to_bill = round(remaining)        (whatever is left)
       Rows with no Pris or to_bill ≤ 0 are skipped (renegotiation can
       leave nothing to invoice — that's a clean skip, not a 0-line draft).
    4. Resolve the Fiken customer:
         Project → Fakturamottaker → Orgnr → /contacts match
         (auto-creates a Fiken contact when no Orgnr match exists).
         If Notion has no Orgnr at all (operator forgot, or the
         Fakturamottaker isn't linked yet) the draft is linked to a
         shared "Mangler kunde" placeholder contact (auto-created on
         first use, cached per company_slug) — operator picks the real
         customer in Fiken's draft UI before clicking Send. Missing
         Orgnr is not a failure.
    5. POST a DRAFT invoice (drafts are NOT sent — the operator reviews
       and clicks Send in Fiken's UI). One line per Oppgave; description
       = "{Navn} — {Beskrivelse}" (collapses to just Navn when Beskrivelse
       is blank). `ourReference` = project name (Vår referanse).
       `yourReference` = project `Faktura merkes` (Deres referanse).
    6. Persist the audit trail (FikenInvoice + FikenInvoiceLine — one
       row per Oppgave actually billed; each line carries the NOK amount
       sent so the cumulative billed-per-row is reconstructable).
    7. Stamp each billed Oppgave in Notion: bump `Fakturert beløp` by
       to_bill, set `Fakturert status` to "Fakturert 50%" (oppstart)
       or "Fakturert" (slutt). Flip the Project `Faktura status` to
       "Oppstart fakturert" or "Fakturert". Write the draft URL onto
       the side-specific Project column.

Idempotency / re-click safety:
  - The dedup index on the `send_faktura` task type collapses concurrent
    double-clicks during the in-flight window to one task.
  - Once the engine flips the Project `Faktura status` past the billable
    states, the next click is a clean skip (no Fiken POST).
  - The audit trail is the source of truth for "what was billed when" —
    each FikenInvoiceLine carries the NOK amount.

Out of scope (v2):
  - Cancelling / deleting drafts (operator does it in Fiken UI).
  - Auto-send (drafts stay drafts until user clicks Send in Fiken).
  - Reading the draft back into Notion before send (Phase C poller).
  - Kategori-based product mapping (client hasn't finalized the kategori
    options yet — the discipline → product mapping stays for now).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import brreg as brreg_client
from gb_automations.clients import fiken as fiken_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    DISCIPLINE_KEYS,
    FAKTURA_STATUS_FULL_DONE,
    FAKTURA_STATUS_OPPSTART_DONE,
    FAKTURA_STATUS_TO_INVOICE_TYPE,
    FAKTURERT_STATUS_50,
    FAKTURERT_STATUS_FULL,
    FAKTURERT_STATUS_IKKE,
    FAKTURERT_STATUS_UTGAR,
    FIKEN_FREE_TEXT_INCOME_ACCOUNT,
    KORREKSJON_KIND_KORREKSJONSRUNDE,
    FAKTURAMOTTAKER_PROPS,
    OPPGAVER_DESC_PROP,
    OPPGAVER_PROPS,
    PROJECTS_FAKTURA_MERKES_PROP,
    PROJECTS_FAKTURA_STATUS_PROP,
    PROJECTS_FAKTURAMOTTAKER_PROP,
    PROJECTS_KUNDER_PROP,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import (
    FikenBankAccount,
    FikenInvoice,
    FikenInvoiceLine,
    FikenPlaceholderContact,
    FikenProductByKategori,
)

logger = logging.getLogger(__name__)


# Norwegian display names used by `_line_product_name` as a final
# fallback when the row has no Kategori AND no Navn/Beskrivelse. Very
# rare in practice; kept inline so the engine never produces an empty
# line description.
_DISCIPLINE_FALLBACK_NAMES = {
    "interior": "Interiør",
    "exterior": "Eksteriør",
    "animation": "Animasjon",
    "other": "Annet",
}

# Hardcoded oppstart split. The brief locked in 50/50; making it
# configurable added an Oppstartsandel % field on every project that
# nobody actually wanted to think about per-click.
OPPSTART_FRACTION = 0.5

# Default payment terms (days from issue date) on a Fiken draft.
# Operator can edit it in Fiken's UI before sending.
DEFAULT_DAYS_UNTIL_DUE_DATE = 14


@dataclass
class InvoiceLine:
    """One Fiken invoice line — one per eligible Oppgave.

    Price/quantity contract (v3 — "Pris is always Pris"):
      - `unit_price_nok_ore` is the FULL agreed Pris from Notion, every
        run, in øre. Oppstart and slutt both send the same unit price;
        only `quantity_fraction` changes between modes.
      - `quantity_fraction` is the fraction of the row to bill on this
        run: 0.5 on oppstart, `remaining/gross` on slutt (1.0 when no
        prior oppstart, < 1.0 when partially billed already). Fiken
        prints it as "Antall" so the customer sees how much of the
        agreed deliverable they're paying for now.
      - Fiken's line math is `quantity × unitPrice × (1 − discount/100)`,
        which lands on the post-rabatt NOK amount we record as
        `amount_nok_ore` (and stamp onto Notion's `Fakturert beløp`).

    Product link (v3 — Kategori → Fiken product):
      - `product_id` is the numeric Fiken product id resolved from the
        Oppgave's `Oppgave kategori` multi-select (first label, name
        match against Fiken's `/products`). When linked, Fiken auto-
        fills the line's `incomeAccount` from the product itself — the
        engine never sends a hardcoded account.
      - `description` (→ Fiken's `description`, the bold first line) is
        the Kategori label when present, falling back to "Navn —
        Beskrivelse" when not.
      - `comment` (→ Fiken's `comment`, the smaller sub-line) is always
        "Navn - Beskrivelse" so the customer reads the category up top
        and the specific deliverable right beneath.

    Free-text fallback: when no Fiken product matches the Kategori label,
    `product_id` is None and the engine sends a free-text line. Fiken
    rejects free-text lines with no `incomeAccount` (400), which the
    operator resolves by adding the missing Fiken product.
    """

    # Canonical discipline key (e.g. "interior") OR None when the row's
    # Type isn't a recognized discipline. Informational on the line
    # dataclass; the engine no longer gates rows on it (bulletproof mode).
    discipline: str | None
    product_id: int | None     # → Fiken `productId` when set; None = free-text
    description: str           # → Fiken `description` (bold first line)
    comment: str               # → Fiken `comment` (sub-line)
    unit_price_nok_ore: int    # → Fiken `unitPrice`; ALWAYS the full Pris in øre
    quantity_fraction: float   # → Fiken `quantity` (Antall)
    discount_percent: float    # 0–100 → Fiken `discount` (per-line, percent)
    amount_nok_ore: int        # post-discount net billed on this line, in øre
    oppgave_page_id: str
    # Convenience: the running-total this row will land at after the
    # stampback (caller computes already + this_run before passing on).
    new_billed_amount_nok: float


@dataclass
class InvoiceCreateResult:
    project_page_id: str
    invoice_type: str | None = None  # filled once Faktura status is read
    action: str = "ok"  # ok | skipped | failed
    note: str | None = None
    eligible_rows: int = 0
    lines_created: int = 0
    fiken_invoice_id: str | None = None
    draft_url: str | None = None
    customer_match: str | None = None
    skipped_rows: list[str] = field(default_factory=list)


# ============================================================
# Pure helpers (unit-tested standalone)
# ============================================================


def _normalize_discipline(raw: str | None) -> str | None:
    """Map a raw `Type` label (e.g. "Interiør") to a canonical key
    ("interior"). Returns None for non-discipline rows.
    """
    if not raw:
        return None
    return DISCIPLINE_KEYS.get(raw.strip().lower())


def _eligible_rows(
    rows: list[dict[str, Any]], invoice_type: str
) -> list[dict[str, Any]]:
    """Filter Notion rows to those eligible for this invoice run.

    "Bulletproof" mode: the engine errs on the side of INCLUDING rows
    so the operator can trust that every Oppgave they see lands on the
    invoice draft unless they explicitly excluded it (`Utgår`) or it's
    an auto-generated admin sub-row (`Korreksjonsrunde`).

    A row is SKIPPED iff:
      - `Type == "Korreksjonsrunde"` — these are auto-created by the
        Frame.io integration to track correction rounds; they're admin
        metadata, not deliverables.
      - `Fakturert status == "Utgår"` — operator excluded it.
      - `Fakturert status == "Fakturert"` — already fully billed,
        nothing to add (Fakturert beløp = gross already; this is a
        nothing-to-bill case, not a hidden filter).
      - For oppstart: `Fakturert status == "Fakturert 50%"` — already
        oppstartet; re-clicking oppstart shouldn't double-bill.
      - For slutt: nothing further; both `Ikke fakturert` AND
        `Fakturert 50%` land. ("Fakturert" is caught above.)

    Everything else passes — including rows with blank Type, unknown
    Type, missing Pris, missing Kategori, etc. Downstream stages handle
    those gracefully (kr 0 unitPrice, free-text with default
    incomeAccount, etc.).
    """
    if invoice_type not in ("oppstart", "slutt"):
        raise ValueError(
            f"invoice_type must be oppstart|slutt, got {invoice_type!r}"
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        # Skip Korreksjonsrunde sub-rows. `task_discipline` returns the
        # raw Type label (e.g. "Interiør", "Klargjøre modell",
        # "Korreksjonsrunde", or None for blank).
        raw_type = (notion_client.task_discipline(row) or "").strip()
        if raw_type.lower() == KORREKSJON_KIND_KORREKSJONSRUNDE.lower():
            continue

        status = (
            notion_client.read_select_name(row, OPPGAVER_PROPS["billed_status"])
            or FAKTURERT_STATUS_IKKE
        )
        if status in (FAKTURERT_STATUS_FULL, FAKTURERT_STATUS_UTGAR):
            continue
        if invoice_type == "oppstart":
            if status == FAKTURERT_STATUS_50:
                continue
        # else slutt: Ikke fakturert AND Fakturert 50% both pass.

        out.append(row)
    return out


def _row_billable_nok(
    row: dict[str, Any], invoice_type: str
) -> tuple[float, float, float, float, float] | None:
    """Compute the per-row billing amounts.

    Returns a 5-tuple
        (to_bill_nok, new_billed_total_nok, unit_price_nok,
         quantity_fraction, discount_fraction)
    or None when the row should be skipped (no Pris, or renegotiation
    pushed remaining ≤ 0). All NOK values are decimals; the integer-øre
    conversion happens in the caller.

    Contract (v3 — "Pris is always Pris"):
      - `unit_price_nok` is ALWAYS the full agreed Pris from Notion,
        regardless of mode. The customer sees the same unit price on
        every invoice; only `quantity_fraction` (Antall) changes between
        oppstart and slutt.
      - `quantity_fraction` is how much of the row to bill on this run.
        Oppstart = 0.5. Slutt = remaining/gross — the proportion of the
        gross still owed, which is 1.0 on a fresh row and 0.5 after a
        clean oppstart. Renegotiation cases (operator drops Pris between
        runs) flow through naturally because we recompute remaining
        against the live `Pris × (1 − Rabatt)`.
      - `to_bill_nok` is the post-rabatt NOK that lands on Notion's
        `Fakturert beløp` and the audit trail. Equals
        `quantity_fraction × Pris × (1 − Rabatt)` — same number Fiken
        computes from the line internally.

    Math:
        gross         = Pris × (1 − Rabatt)       # Rabatt is a fraction (0.15 = 15%)
        already       = Fakturert beløp (running total Notion holds)
        remaining     = gross − already
        oppstart  →   quantity_fraction = 0.5
                      to_bill           = round(gross × 0.5)
        slutt     →   quantity_fraction = remaining / gross   (0 < q ≤ 1; 0 when
                                                                gross=0)
                      to_bill           = round(remaining)
        unit_price    = Pris             (always; pre-rabatt, full agreed price)
        new_total     = already + to_bill

    Bulletproof mode: missing/zero Pris is treated as Pris=0, NOT a
    skip. The engine emits a kr 0 line so the operator sees the row on
    the draft and can fill in the Pris later. Only the genuine
    "nothing to bill" case (slutt with remaining ≤ 0 — operator
    lowered Pris below what was already invoiced) returns None; that
    one is silently skipped because Notion's `Budsjett` vs `Fakturert
    sum` rollups already make the over-billing visible at project
    level (and credit-note logic is out of scope for this engine).
    """
    raw_price = notion_client.read_number_prop(
        row, OPPGAVER_PROPS["price_per_row"]
    )
    # Bulletproof: missing or non-positive Pris becomes 0. The row still
    # lands on the draft as a kr 0 line so the operator can fix the Pris
    # in Notion and re-create the draft, OR edit the line directly in
    # Fiken's UI.
    if raw_price is None or raw_price <= 0:
        logger.info(
            "fiken send-faktura: row %s has no Pris set — sending kr 0 "
            "line (operator can fix Pris in Notion and re-click, or edit "
            "the line in Fiken's UI)",
            row.get("id"),
        )
        price_nok = 0.0
    else:
        price_nok = float(raw_price)

    # Rabatt: Notion's "Percent" number format returns the value as a
    # fraction (0.15 for 15%), NOT as a percent integer (15). The Oppgaver
    # `Rabatt` column is modeled as Percent so operators see the `%` suffix
    # in Notion's UI — the engine just consumes the fraction directly. If
    # someone later flips the column to plain Number they'll see this
    # under-discount and have to flip it back.
    discount_fraction = (
        notion_client.read_number_prop(row, OPPGAVER_PROPS["discount_pct"])
        or 0.0
    )
    discount_fraction = max(0.0, min(float(discount_fraction), 1.0))
    gross = price_nok * (1.0 - discount_fraction)

    already = (
        notion_client.read_number_prop(row, OPPGAVER_PROPS["billed_amount"])
        or 0.0
    )
    already = max(0.0, float(already))

    if invoice_type == "oppstart":
        quantity_fraction = OPPSTART_FRACTION
        to_bill = round(gross * OPPSTART_FRACTION)
    else:
        remaining = gross - already
        # gross == 0 → land as a kr 0 line (quantity_fraction = 0 so
        # Fiken still creates the line cleanly).
        if gross <= 0:
            quantity_fraction = 0.0
            to_bill = 0
        elif remaining <= 0:
            # Genuine over-billed case: operator lowered Pris below what
            # was already invoiced. Skip silently — the over-bill is
            # already visible in Notion via the `Budsjett` vs `Fakturert
            # sum` rollups, and a credit note (future feature) is the
            # proper resolution.
            logger.info(
                "fiken send-faktura: row %s already over-billed "
                "(gross=%.2f, already=%.2f, remaining=%.2f) — skipping; "
                "Notion rollups surface the discrepancy",
                row.get("id"),
                gross,
                already,
                remaining,
            )
            return None
        else:
            quantity_fraction = remaining / gross
            to_bill = round(remaining)

    return (
        float(to_bill),
        already + float(to_bill),
        price_nok,              # unit_price ALWAYS = Pris (0 when blank)
        float(quantity_fraction),
        discount_fraction,
    )


def _resolve_kategori_label(row: dict[str, Any]) -> str | None:
    """Read `Oppgave kategori` (multi_select) and return the FIRST label.

    Notion preserves the operator's selection order, so the first label
    is their primary intent. Returns None when the column is blank —
    caller then falls through to the free-text / Navn-Beskrivelse path.
    """
    labels = notion_client.read_multi_select_names(
        row, OPPGAVER_PROPS["kategori"]
    )
    return labels[0] if labels else None


async def _resolve_kategori_to_product_id(
    company_slug: str, kategori_label: str
) -> int | None:
    """Map a Notion `Oppgave kategori` label to a Fiken `productId`.

    Lookup path:
      1. Postgres cache (`fiken_product_by_kategori`) keyed on
         (company_slug, kategori_label). Hit → return cached id.
      2. Miss → list Fiken's products and find the one whose `name`
         equals `kategori_label` exactly. When multiple match, prefer
         the active one (operators sometimes leave a deactivated
         duplicate). Cache the (kategori_label → productId) mapping.
      3. No name match → return None. Caller logs a WARN and sends the
         line free-text; Fiken then 400s on the missing incomeAccount,
         which is the operator's prompt to add the product in Fiken.

    The cache is a pure speedup: we could rebuild it from Fiken's
    catalogue at any time. There's no self-heal for stale entries (a
    Fiken-side delete leaves a dangling id in our cache); a 400 from
    Fiken on the next draft surfaces the gap.
    """
    if not kategori_label:
        return None

    async with SessionLocal() as session:
        cached = await session.get(
            FikenProductByKategori,
            {"company_slug": company_slug, "kategori_label": kategori_label},
        )
    if cached is not None:
        try:
            return int(cached.fiken_product_id)
        except (TypeError, ValueError):
            logger.warning(
                "fiken: cached productId %r for kategori %r is not an int "
                "— re-resolving against Fiken",
                cached.fiken_product_id,
                kategori_label,
            )

    try:
        products = await fiken_client.list_products(company_slug)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken: list_products failed while resolving kategori %r",
            kategori_label,
        )
        return None

    # Match by name (case-sensitive, exact). Prefer active products if
    # multiple share a name.
    matches = [
        p for p in products if (p.get("name") or "") == kategori_label
    ]
    if not matches:
        return None
    active = [p for p in matches if p.get("active", True)]
    chosen = active[0] if active else matches[0]
    raw_id = chosen.get("productId") or chosen.get("id")
    try:
        product_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        product_id = None
    if product_id is None:
        logger.warning(
            "fiken: product %r matched kategori but has no usable productId "
            "(%s)",
            kategori_label,
            chosen,
        )
        return None

    async with SessionLocal() as session:
        await session.execute(
            pg_insert(FikenProductByKategori)
            .values(
                company_slug=company_slug,
                kategori_label=kategori_label,
                fiken_product_id=str(product_id),
                name_when_cached=chosen.get("name") or kategori_label,
            )
            .on_conflict_do_update(
                index_elements=["company_slug", "kategori_label"],
                set_={
                    "fiken_product_id": str(product_id),
                    "name_when_cached": chosen.get("name") or kategori_label,
                },
            )
        )
        await session.commit()
    return product_id


def _line_product_name(row: dict[str, Any], kategori: str | None) -> str:
    """Compose the bold first-line product name shown on the Fiken invoice.

    Returns the Kategori label when present (e.g. "Næring - Interiør").
    When Kategori is missing, falls back to the legacy shape so existing
    rows without a Kategori still produce a readable line:
        Navn — Beskrivelse   (both set)
        Navn                  (Beskrivelse blank)
        Beskrivelse           (Navn blank; unlikely in practice)
        discipline display    (all blank)
    """
    if kategori:
        return kategori
    title = (notion_client.extract_page_title(row) or "").strip()
    description = (
        notion_client.read_rich_text_prop(row, OPPGAVER_DESC_PROP) or ""
    ).strip()
    if title and description:
        return f"{title} — {description}"
    if title:
        return title
    if description:
        return description
    canonical = _normalize_discipline(notion_client.task_discipline(row))
    if canonical:
        return _DISCIPLINE_FALLBACK_NAMES.get(canonical, canonical.title())
    return ""


def _line_comment(row: dict[str, Any]) -> str:
    """Compose the smaller `comment` sub-line shown below the product name.

    Format: "Navn - Beskrivelse" when both are present, just Navn when
    Beskrivelse is blank, just Beskrivelse when Navn is blank. Empty
    string when both blank — the line has no sub-text and Fiken renders
    just the product name.
    """
    title = (notion_client.extract_page_title(row) or "").strip()
    description = (
        notion_client.read_rich_text_prop(row, OPPGAVER_DESC_PROP) or ""
    ).strip()
    if title and description:
        return f"{title} - {description}"
    if title:
        return title
    if description:
        return description
    return ""


async def _build_line_items(
    eligible: list[dict[str, Any]],
    *,
    invoice_type: str,
    company_slug: str,
) -> list[InvoiceLine]:
    """Compose Fiken invoice lines from eligible rows — ONE line per
    Oppgave. Per-line amount + quantity come from `_row_billable_nok`;
    the product link comes from `_resolve_kategori_to_product_id`.

    Bulletproof: every eligible row (passed by _eligible_rows) lands as
    a line. The only `None` return from _row_billable_nok is the
    genuine over-billed case (slutt with remaining ≤ 0 — operator
    lowered Pris below Fakturert beløp); that one is intentionally
    skipped so the over-bill stays visible only in Notion's rollups.

    Rows with missing Type, missing Pris, missing Kategori, etc. ALL
    land:
      - Missing Type → discipline=None on the InvoiceLine; description
        falls back to Navn — Beskrivelse (and ultimately to the row id
        if even Navn is blank). The Fiken line still renders.
      - Missing Pris → kr 0 unit price; line shows on the draft as a
        zero-amount line that the operator can edit in Fiken or fix in
        Notion + re-click.
      - Missing Kategori (or Kategori with no Fiken product match) →
        free-text line with the default `incomeAccount` (handled at
        payload assembly).

    Stable order: preserves the order Notion returned them.
    Async because the kategori → productId path hits Fiken on cache miss.
    """
    lines: list[InvoiceLine] = []
    for row in eligible:
        # `_normalize_discipline` returns None for rows whose Type isn't
        # in DISCIPLINE_KEYS. We no longer reject those rows — discipline
        # is just informational on the line dataclass now, used for audit
        # trail / fallback labels.
        canonical = _normalize_discipline(notion_client.task_discipline(row))

        amounts = _row_billable_nok(row, invoice_type)
        if amounts is None:
            # _row_billable_nok only returns None in the over-billed
            # case (slutt with remaining ≤ 0). Skip silently — Notion
            # rollups surface the mismatch.
            continue
        (
            to_bill_nok,
            new_total_nok,
            unit_price_nok,
            quantity_fraction,
            discount_fraction,
        ) = amounts
        amount_ore = int(round(to_bill_nok * 100))
        unit_price_ore = int(round(unit_price_nok * 100))
        # kr 0 lines land — bulletproof. Negative would be a bug; clamp.
        if amount_ore < 0:
            amount_ore = 0
        if unit_price_ore < 0:
            unit_price_ore = 0

        kategori = _resolve_kategori_label(row)
        product_id: int | None = None
        if kategori:
            product_id = await _resolve_kategori_to_product_id(
                company_slug, kategori
            )
            if product_id is None:
                logger.warning(
                    "fiken send-faktura: row %s has Oppgave kategori %r "
                    "but no Fiken product with that exact name exists "
                    "— sending as free-text with default incomeAccount "
                    "(add the product in Fiken's UI to route it cleanly)",
                    row.get("id"),
                    kategori,
                )
        # Description fallback chain: Kategori → Navn — Beskrivelse →
        # discipline display → row id (so the line is never empty).
        description = _line_product_name(row, kategori)
        if not description and canonical:
            description = _DISCIPLINE_FALLBACK_NAMES.get(
                canonical, canonical.title()
            )
        if not description:
            description = row.get("id", "") or "(no description)"
        comment = _line_comment(row)
        lines.append(
            InvoiceLine(
                discipline=canonical,
                product_id=product_id,
                description=description,
                comment=comment,
                unit_price_nok_ore=unit_price_ore,
                quantity_fraction=round(quantity_fraction, 6),
                discount_percent=round(discount_fraction * 100.0, 4),
                amount_nok_ore=amount_ore,
                oppgave_page_id=row.get("id", ""),
                new_billed_amount_nok=new_total_nok,
            )
        )
    return lines


def _normalize_orgnr(raw: str | None) -> str:
    """Strip every non-digit so '123 456 789' compares equal to '123456789'.
    Returns "" if input is empty or contains no digits.
    """
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


# ============================================================
# Notion-side: read Fakturamottaker → Orgnr off the project (one hop)
# ============================================================


async def _resolve_project_orgnr(
    project_page: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Read the project's direct `Fakturamottaker` relation → Orgnr.

    Returns (orgnr_digits_only, fakturamottaker_title). The title is the
    Notion Fakturamottaker row's name (the billing entity) — used by the
    engine to auto-create a Fiken contact when no matching customer
    exists yet. That name is what the customer will see on the invoice,
    so it should be the billing entity, not a parent Kunder.

    The earlier two-hop walk (Project → Kunder → Fakturamottaker) is gone:
    operators now set Fakturamottaker directly on each Project so the
    billing recipient can differ from the parent Kunder (e.g. a property-
    management company billing a per-building sub-LLC). Kunder may still
    exist on the Projects DB for contact / view purposes; the engine no
    longer reads it.
    """
    fakt_ids = notion_client.read_relation_ids(
        project_page, PROJECTS_FAKTURAMOTTAKER_PROP
    )
    if not fakt_ids:
        return None, None

    first_fakt_name: str | None = None
    for fakt_id in fakt_ids:
        try:
            fakt_page = await notion_client.get_page(fakt_id)
        except Exception as err:  # noqa: BLE001
            logger.info(
                "fiken: skipping Fakturamottaker %s (read failed: %s)",
                fakt_id,
                err,
            )
            continue
        fakt_name = (
            notion_client.extract_page_title(fakt_page) or ""
        ).strip() or None
        if first_fakt_name is None:
            first_fakt_name = fakt_name

        raw = notion_client.read_rich_text_prop(
            fakt_page, FAKTURAMOTTAKER_PROPS["orgnr"]
        )
        normalized = _normalize_orgnr(raw)
        if normalized:
            return normalized, fakt_name
    return None, first_fakt_name


# ============================================================
# Placeholder customer ("Mangler kunde") — one per Fiken company
# ============================================================


async def _ensure_placeholder_contact(company_slug: str) -> int | None:
    """Return the cached placeholder-contact id for `company_slug`, creating
    the Fiken contact on first call.

    Used when the project has no Orgnr to resolve a real customer with —
    we link the draft to a single shared "Mangler kunde" contact so the
    operator can pick or create the real customer in Fiken's UI before
    clicking Send. One placeholder per company_slug, cached in
    `fiken_placeholder_contacts`.

    Returns the integer contactId or None on Fiken errors. The caller is
    expected to treat None as "fail the task" — a placeholder we can't
    create or read is the only way this path still hard-fails.
    """
    placeholder_name = settings.fiken_placeholder_contact_name

    async with SessionLocal() as session:
        cached = await session.get(FikenPlaceholderContact, company_slug)

    if cached is not None:
        try:
            return int(cached.fiken_contact_id)
        except (TypeError, ValueError):
            logger.warning(
                "fiken: cached placeholder contactId %r is not an int — "
                "re-creating",
                cached.fiken_contact_id,
            )

    # Cache miss (or unparseable cached id). Create the Fiken contact
    # with no Orgnr. customer=True so it shows up in list_contacts and
    # can be selected from Fiken's draft UI.
    try:
        created = await fiken_client.create_contact(
            company_slug,
            name=placeholder_name,
            organization_number=None,
            customer=True,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken: failed to create placeholder contact %r for slug=%s",
            placeholder_name,
            company_slug,
        )
        return None

    raw_id = created.get("contactId") or created.get("id")
    try:
        new_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        new_id = None
    if new_id is None:
        logger.warning(
            "fiken: created placeholder contact carries no contactId/id "
            "(%s) — Fiken's response shape changed?",
            created,
        )
        return None

    async with SessionLocal() as session:
        await session.execute(
            pg_insert(FikenPlaceholderContact)
            .values(
                company_slug=company_slug,
                fiken_contact_id=str(new_id),
                name_when_created=placeholder_name,
            )
            .on_conflict_do_update(
                index_elements=["company_slug"],
                set_={
                    "fiken_contact_id": str(new_id),
                    "name_when_created": placeholder_name,
                },
            )
        )
        await session.commit()
    return new_id


# ============================================================
# Bank account (Kontonummer on the printed invoice)
# ============================================================


async def _ensure_bank_account_number(company_slug: str) -> str | None:
    """Resolve the bank account number to send on this draft.

    Lookup precedence:
      1. `settings.fiken_bank_account_number` (env override) — pinned by
         the operator. Bypasses cache entirely; useful when the company
         has multiple accounts and the operator wants a specific one.
      2. Postgres cache `fiken_bank_accounts(company_slug)` — auto-
         populated on the first send_faktura per company.
      3. Miss → `fiken_client.list_bank_accounts`, pick the first
         account where `inactive=False` and `type=="normal"`, cache
         its bankAccountNumber.

    Returns None when nothing is set / nothing resolves. The caller
    then omits `bankAccountNumber` from the draft body and Fiken's
    printed invoice falls back to whatever it would normally pick (or
    blank).
    """
    if settings.fiken_bank_account_number:
        return settings.fiken_bank_account_number

    async with SessionLocal() as session:
        cached = await session.get(FikenBankAccount, company_slug)
    if cached is not None and cached.bank_account_number:
        return cached.bank_account_number

    try:
        accounts = await fiken_client.list_bank_accounts(company_slug)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken: list_bank_accounts failed for slug=%s — draft will be "
            "sent without a Kontonummer",
            company_slug,
        )
        return None

    chosen = next(
        (
            a
            for a in accounts
            if not a.get("inactive", False) and a.get("type") == "normal"
        ),
        None,
    )
    if chosen is None:
        logger.warning(
            "fiken: no active normal-type bank account found for slug=%s "
            "— draft will be sent without a Kontonummer (set "
            "FIKEN_BANK_ACCOUNT_NUMBER in .env to pin one explicitly)",
            company_slug,
        )
        return None

    number = (chosen.get("bankAccountNumber") or "").strip()
    if not number:
        logger.warning(
            "fiken: matched bank account has no bankAccountNumber (%s)",
            chosen,
        )
        return None

    async with SessionLocal() as session:
        await session.execute(
            pg_insert(FikenBankAccount)
            .values(
                company_slug=company_slug,
                bank_account_number=number,
                name_when_cached=chosen.get("name") or number,
            )
            .on_conflict_do_update(
                index_elements=["company_slug"],
                set_={
                    "bank_account_number": number,
                    "name_when_cached": chosen.get("name") or number,
                },
            )
        )
        await session.commit()
    return number


# ============================================================
# Brreg-driven customer enrichment
# ============================================================
#
# Two paths land here:
#
#   _brreg_enrich_name(orgnr, fallback): used when Notion already has
#     an Orgnr. Returns Brreg's official navn (so the Fiken auto-create
#     gets a clean legal name instead of whatever the operator typed).
#     Best-effort: Brreg failure → use the fallback.
#
#   _resolve_project_via_kunder_brreg(project_page): used when Notion
#     has no Orgnr but the Project's Kunder relation has a name. Walks
#     Project → Kunder, searches Brreg, applies the strict suffix-aware
#     match, returns (orgnr, brreg_dict) on a clean win. Otherwise
#     (None, None) — falls back to the placeholder path.
#
#   _update_fakturamottaker_in_notion(project_page, orgnr, brreg_name):
#     post-creation writeback. Updates the existing Fakturamottaker title
#     to Brreg's name (idempotent — skip if equal) and fills its Orgnr
#     if blank; or creates a fresh Fakturamottaker row and links it to
#     the Project when the Project had nothing linked.
#
# All three are best-effort: they log warnings + return on any Notion
# error so the in-flight draft (already created in Fiken at this point)
# stays the source of truth.


async def _brreg_enrich_name(
    orgnr: str, fallback_name: str | None
) -> tuple[str | None, dict[str, Any] | None]:
    """Return Brreg's official `navn` for an Orgnr, plus the full payload.

    Returns (best_name, brreg_dict) where:
      - best_name = Brreg's navn on success; the fallback_name otherwise
        (or None if both are missing).
      - brreg_dict = the Brreg entity payload on success; None on miss.

    Splitting the two return values lets the caller distinguish "we
    have a Brreg record, write it back to Notion" from "Brreg miss,
    just use this name to create the Fiken contact" — we don't want
    to overwrite Notion data with a fallback name we made up.
    """
    enhet = await brreg_client.get_enhet(orgnr)
    if not enhet:
        return (fallback_name or None), None
    brreg_navn = (enhet.get("navn") or "").strip() or None
    if not brreg_navn:
        return (fallback_name or None), None
    return brreg_navn, enhet


async def _resolve_project_via_kunder_brreg(
    project_page: dict[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """For projects with no Orgnr: walk Project → Kunder → search Brreg.

    Returns (orgnr_digits, brreg_dict) on a clean suffix-aware match,
    else (None, None). Falls back silently on every degraded path:
      - No Kunder relation on the project,
      - Kunder row read failed,
      - Kunder title blank,
      - Brreg search returned 0 or 2+ clean matches.

    The clean-match rule (case-insensitive, suffix-aware) lives in
    brreg_client.pick_exact_match.
    """
    kunder_ids = notion_client.read_relation_ids(
        project_page, PROJECTS_KUNDER_PROP
    )
    if not kunder_ids:
        return None, None

    # Walk the first Kunder row with a usable title. Operators usually
    # link a project to one Kunder; a multi-link is rare but we cope.
    for kunder_id in kunder_ids:
        try:
            kunder_page = await notion_client.get_page(kunder_id)
        except Exception as err:  # noqa: BLE001
            logger.info(
                "fiken: skipping Kunder %s during Brreg resolve "
                "(read failed: %s)",
                kunder_id,
                err,
            )
            continue
        kunder_name = (
            notion_client.extract_page_title(kunder_page) or ""
        ).strip()
        if not kunder_name:
            continue

        results = await brreg_client.search_enheter(kunder_name)
        chosen = brreg_client.pick_exact_match(kunder_name, results)
        if chosen is None:
            logger.info(
                "fiken: Brreg search for Kunder %r returned %d hit(s) but "
                "no clean suffix-aware match — falling back to placeholder",
                kunder_name,
                len(results),
            )
            return None, None
        orgnr = _normalize_orgnr(chosen.get("organisasjonsnummer"))
        if not orgnr:
            logger.warning(
                "fiken: matched Brreg result for Kunder %r has no "
                "organisasjonsnummer (%s)",
                kunder_name,
                chosen,
            )
            return None, None
        logger.info(
            "fiken: Brreg resolved Kunder %r → orgnr=%s navn=%r",
            kunder_name,
            orgnr,
            chosen.get("navn"),
        )
        return orgnr, chosen
    return None, None


async def _update_fakturamottaker_in_notion(
    project_page: dict[str, Any], orgnr: str, brreg_name: str
) -> None:
    """Idempotently align Notion's Fakturamottaker with Brreg's record.

    Three sub-cases:
      1. Project has a Fakturamottaker linked AND that row's Orgnr is
         non-blank → only update the title if it differs from
         brreg_name. (Don't fill Orgnr — we already had one.)
      2. Project has a Fakturamottaker linked AND that row's Orgnr is
         blank → fill Orgnr=orgnr AND update title to brreg_name.
      3. Project has NO Fakturamottaker linked → create a new row in
         FAKTURAMOTTAKER_DB_ID with title=brreg_name + Orgnr=orgnr,
         then PATCH the Project's relation to point at it.

    Best-effort: every Notion API error here logs WARN + returns. The
    Fiken draft has already been created at this point; a failed
    writeback just leaves Notion slightly stale.
    """
    project_page_id = project_page.get("id") or ""

    fakt_ids = notion_client.read_relation_ids(
        project_page, PROJECTS_FAKTURAMOTTAKER_PROP
    )
    if fakt_ids:
        # Case 1 or 2 — update the existing row.
        fakt_id = fakt_ids[0]
        try:
            fakt_page = await notion_client.get_page(fakt_id)
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "fiken Brreg writeback: failed to GET Fakturamottaker "
                "%s: %s",
                fakt_id,
                err,
            )
            return

        current_title = (
            notion_client.extract_page_title(fakt_page) or ""
        ).strip()
        current_orgnr = _normalize_orgnr(
            notion_client.read_rich_text_prop(
                fakt_page, FAKTURAMOTTAKER_PROPS["orgnr"]
            )
        )

        # Title update (idempotent — skip if already equal).
        if current_title != brreg_name:
            try:
                await notion_client.set_page_title(
                    fakt_id,
                    title=brreg_name,
                    title_prop_name=FAKTURAMOTTAKER_PROPS["navn"],
                )
                logger.info(
                    "fiken Brreg writeback: renamed Fakturamottaker %s "
                    "%r → %r",
                    fakt_id,
                    current_title,
                    brreg_name,
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    "fiken Brreg writeback: failed to rename Fakturamottaker "
                    "%s to %r: %s",
                    fakt_id,
                    brreg_name,
                    err,
                )

        # Orgnr fill (only when blank — don't clobber an operator-set value).
        if not current_orgnr and orgnr:
            try:
                await _set_fakturamottaker_orgnr(fakt_id, orgnr)
                logger.info(
                    "fiken Brreg writeback: filled Orgnr=%s on "
                    "Fakturamottaker %s",
                    orgnr,
                    fakt_id,
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    "fiken Brreg writeback: failed to fill Orgnr on "
                    "Fakturamottaker %s: %s",
                    fakt_id,
                    err,
                )
        return

    # Case 3 — no Fakturamottaker linked. Create one and link it. Skip
    # silently if the engine doesn't know the Fakturamottaker DB id.
    db_id = settings.fakturamottaker_db_id
    if not db_id:
        logger.info(
            "fiken Brreg writeback: project %s has no Fakturamottaker "
            "linked AND FAKTURAMOTTAKER_DB_ID is unset — skipping "
            "creation. Set FAKTURAMOTTAKER_DB_ID in .env to enable the "
            "Brreg-by-name writeback path.",
            project_page_id,
        )
        return

    try:
        new_fakt_id = await notion_client.create_page_in_db(
            db_id,
            title=brreg_name,
            title_prop_name=FAKTURAMOTTAKER_PROPS["navn"],
            extra_properties={
                FAKTURAMOTTAKER_PROPS["orgnr"]: {
                    "rich_text": [{"text": {"content": orgnr}}]
                },
            },
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "fiken Brreg writeback: failed to create Fakturamottaker "
            "(navn=%r, orgnr=%s) in DB %s: %s",
            brreg_name,
            orgnr,
            db_id,
            err,
        )
        return
    try:
        await notion_client.set_relation(
            project_page_id,
            prop_name=PROJECTS_FAKTURAMOTTAKER_PROP,
            target_ids=[new_fakt_id],
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "fiken Brreg writeback: created Fakturamottaker %s but "
            "failed to link it to Project %s: %s",
            new_fakt_id,
            project_page_id,
            err,
        )
        return
    logger.info(
        "fiken Brreg writeback: created Fakturamottaker %s (navn=%r, "
        "orgnr=%s) and linked to Project %s",
        new_fakt_id,
        brreg_name,
        orgnr,
        project_page_id,
    )


async def _set_fakturamottaker_orgnr(fakt_id: str, orgnr: str) -> None:
    """PATCH the `Orgnr` rich_text property on a Fakturamottaker row.

    Tiny helper because Notion's API expects a specific shape for
    rich_text and we don't have a generic set-rich-text helper today.
    Inlined here rather than in clients/notion.py because it's the
    only caller.
    """
    props = {
        FAKTURAMOTTAKER_PROPS["orgnr"]: {
            "rich_text": [{"text": {"content": orgnr}}]
        },
    }
    async with notion_client._client() as client:  # noqa: SLF001
        response = await notion_client._with_retries(  # noqa: SLF001
            lambda: client.patch(
                f"/pages/{fakt_id}", json={"properties": props}
            ),
            op_name=f"PATCH /pages/{fakt_id} Orgnr",
        )
        notion_client._raise_for_status(response)  # noqa: SLF001


# ============================================================
# Top-level engine entrypoint
# ============================================================


async def create_fiken_invoice(project_page_id: str) -> InvoiceCreateResult:
    """Worker entrypoint for a `send_faktura` task.

    Returns a dataclass the worker handler reads to decide done vs.
    failed. Any exception means "retry with backoff"; an `action` of
    "failed" without an exception means "terminal misconfig — don't
    retry into the same brick wall" (e.g. no Orgnr in Notion — operator
    must fix before retry helps).
    """
    result = InvoiceCreateResult(project_page_id=project_page_id)

    if not settings.fiken_company_slug:
        result.action = "failed"
        result.note = "FIKEN_COMPANY_SLUG is not configured"
        return result
    company_slug = settings.fiken_company_slug

    # 1. Read live project page + map Faktura status → invoice_type.
    try:
        project_page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "send_faktura: failed to GET project %s", project_page_id
        )
        result.action = "failed"
        result.note = f"Notion get_page (project): {err}"
        return result

    project_title = (notion_client.extract_page_title(project_page) or "").strip()
    faktura_status = notion_client.read_select_name(
        project_page, PROJECTS_FAKTURA_STATUS_PROP
    )
    invoice_type = FAKTURA_STATUS_TO_INVOICE_TYPE.get(faktura_status or "")
    if not invoice_type:
        logger.info(
            "send_faktura: project %r has Faktura status %r — not a "
            "billable state, skipping",
            project_title or project_page_id,
            faktura_status,
        )
        result.action = "skipped"
        result.note = (
            f"Faktura status {faktura_status!r} is not a billable state "
            "(expected 'Til oppstartsfaktura' or 'Til avslutningsfaktura')"
        )
        return result
    result.invoice_type = invoice_type

    # Deres referanse, optional.
    your_reference = (
        notion_client.read_rich_text_prop(
            project_page, PROJECTS_FAKTURA_MERKES_PROP
        )
        or ""
    ).strip() or None

    # 2. Read all Oppgaver under this project + filter.
    try:
        rows = await notion_client.oppgaver_for_project(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "send_faktura: failed to list oppgaver for %s", project_page_id
        )
        result.action = "failed"
        result.note = f"Notion oppgaver_for_project: {err}"
        return result

    eligible = _eligible_rows(rows, invoice_type)
    result.eligible_rows = len(eligible)
    if not eligible:
        logger.info(
            "send_faktura: no eligible Oppgaver for %s (%s) — nothing to bill",
            invoice_type,
            project_title or project_page_id,
        )
        result.action = "skipped"
        result.note = "no eligible rows"
        return result

    # 3. Build invoice lines. Per-row math is pure; the kategori →
    # productId lookup may hit Fiken on cache miss, so this is async.
    lines = await _build_line_items(
        eligible,
        invoice_type=invoice_type,
        company_slug=company_slug,
    )
    if not lines:
        result.action = "skipped"
        result.note = "no priced rows"
        return result
    result.lines_created = len(lines)

    # 4. Customer resolution. Project → Fakturamottaker → Orgnr, with
    # Brreg fallbacks:
    #
    #   - If Notion has no Orgnr but the Project has a Kunder relation
    #     with a name → search Brreg, accept only a strict suffix-aware
    #     match. On a clean win: recover the Orgnr, write the result
    #     back into Notion (create or fill the Fakturamottaker row),
    #     and proceed through the normal Orgnr path.
    #   - When Orgnr IS present, the Fiken auto-create on a /contacts
    #     miss uses Brreg's official navn instead of the Fakturamottaker
    #     title (and PATCHes Notion's title to match — one-time
    #     enrichment).
    #   - Brreg is best-effort throughout: timeout / 5xx / no clean
    #     match → fall back to today's "Mangler kunde" placeholder.
    orgnr, fakturamottaker_name = await _resolve_project_orgnr(project_page)

    # Track the Brreg payload so we can write the official name back to
    # Notion AFTER the Fiken draft is created. We only writeback when we
    # actually got a Brreg payload — never with a made-up fallback name.
    brreg_payload: dict[str, Any] | None = None

    if not orgnr:
        kunder_orgnr, brreg_via_kunder = await _resolve_project_via_kunder_brreg(
            project_page
        )
        if kunder_orgnr and brreg_via_kunder is not None:
            orgnr = kunder_orgnr
            fakturamottaker_name = (brreg_via_kunder.get("navn") or "").strip()
            brreg_payload = brreg_via_kunder

    customer_id: int | None = None
    customer_label = "<no customer linked>"

    if orgnr:
        try:
            contacts = await fiken_client.list_contacts(
                company_slug, customer=True
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "send_faktura: list_contacts failed for slug=%s", company_slug
            )
            result.action = "failed"
            result.note = f"Fiken list_contacts: {err}"
            return result

        target = orgnr  # already digits-only
        for contact in contacts:
            candidate = _normalize_orgnr(contact.get("organizationNumber"))
            if candidate and candidate == target:
                raw_id = contact.get("contactId") or contact.get("id")
                try:
                    customer_id = int(raw_id) if raw_id is not None else None
                except (TypeError, ValueError):
                    customer_id = None
                customer_label = contact.get("name") or orgnr
                break

        # Auto-create on miss — only when we HAVE an Orgnr to attach.
        # An auto-created contact with no Orgnr would clutter Fiken and
        # risk duplicates next time the same customer's Fakturamottaker
        # gets an Orgnr filled in.
        if customer_id is None:
            # Brreg-by-orgnr enrichment: when we got here via the normal
            # Orgnr path (not via Kunder→Brreg, which already gave us a
            # brreg_payload), look up the official name now so the Fiken
            # contact carries Brreg's record instead of the operator's
            # potentially-stale Fakturamottaker title.
            if brreg_payload is None:
                brreg_name, brreg_payload = await _brreg_enrich_name(
                    orgnr, fakturamottaker_name
                )
            else:
                brreg_name = brreg_payload.get("navn") or fakturamottaker_name
            new_name = (
                brreg_name or fakturamottaker_name or project_title
                or f"Kunde {orgnr}"
            ).strip()
            logger.info(
                "send_faktura: orgnr %s not in Fiken customers — "
                "auto-creating Fiken contact %r (Brreg=%s)",
                orgnr,
                new_name,
                "hit" if brreg_payload is not None else "miss",
            )
            try:
                created = await fiken_client.create_contact(
                    company_slug,
                    name=new_name,
                    organization_number=orgnr,
                    customer=True,
                )
            except Exception as err:  # noqa: BLE001
                logger.exception(
                    "send_faktura: failed to auto-create Fiken contact "
                    "(orgnr=%s, name=%r)",
                    orgnr,
                    new_name,
                )
                result.action = "failed"
                result.note = f"Fiken create_contact: {err}"
                return result
            raw_id = created.get("contactId") or created.get("id")
            try:
                customer_id = int(raw_id) if raw_id is not None else None
            except (TypeError, ValueError):
                customer_id = None
            customer_label = f"{new_name} (auto-created)"
            if customer_id is None:
                logger.warning(
                    "send_faktura: auto-created contact carries no "
                    "contactId/id (%s) — Fiken's response shape changed?",
                    created,
                )
                result.action = "failed"
                result.note = "auto-created Fiken contact has no contactId"
                return result
    else:
        # No Orgnr on Notion side. Link the draft to a shared "Mangler
        # kunde" placeholder contact (auto-created on first use, cached
        # per company_slug). Fiken's API rejects drafts with no
        # customerId, so linking to a placeholder is the only path that
        # keeps the draft creatable. The operator picks or creates the
        # real customer in Fiken's draft UI before clicking Send. The
        # Fakturamottaker name (if any) goes in the log so the operator
        # knows which Notion row needs an Orgnr later.
        placeholder_id = await _ensure_placeholder_contact(company_slug)
        if placeholder_id is None:
            result.action = "failed"
            result.note = (
                "could not create or read the placeholder Fiken contact "
                f"({settings.fiken_placeholder_contact_name!r})"
            )
            return result
        customer_id = placeholder_id
        customer_label = (
            f"{settings.fiken_placeholder_contact_name} (placeholder)"
        )
        logger.info(
            "send_faktura: project %r has no Orgnr (Fakturamottaker=%r) — "
            "linking draft to placeholder contact %r (id=%s); pick the real "
            "customer in Fiken before sending",
            project_title,
            fakturamottaker_name,
            settings.fiken_placeholder_contact_name,
            placeholder_id,
        )

    result.customer_match = customer_label

    # 5. Build line payloads. Product-linked lines whenever we have a
    # `productId` from the Kategori match; free-text fallback otherwise.
    #
    # Field map (product-linked, the happy path):
    #  - `productId`     ← line.product_id (Fiken auto-fills incomeAccount
    #                     from the product itself; we never send the account)
    #  - `description`   ← line.description (Kategori label; mirrors the
    #                     product name — Fiken would auto-fill the same
    #                     value if we omitted it, so sending it is just
    #                     belt-and-braces)
    #  - `comment`       ← line.comment ("Navn - Beskrivelse", per-row context)
    #  - `quantity`      ← line.quantity_fraction (Antall; 0.5 oppstart,
    #                     remaining/gross slutt)
    #  - `unitPrice`     ← line.unit_price_nok_ore (FULL Pris in øre, ALWAYS)
    #  - `vatType`       ← "HIGH" (25% MVA; product carries its own vatType
    #                     too, but we send ours for explicitness)
    #  - `discount`      ← line.discount_percent (percent 0–100; omitted at 0)
    #
    # Free-text fallback (no productId match): same shape minus
    # `productId`. Fiken will 400 with "incomeAccount is required for
    # free-text lines (lines without a productId)". That's deliberate —
    # the operator's prompt to add the missing product in Fiken.
    line_payloads: list[dict[str, Any]] = []
    for line in lines:
        payload: dict[str, Any] = {
            "description": line.description,
            "comment": line.comment,
            "quantity": line.quantity_fraction,
            "unitPrice": line.unit_price_nok_ore,
            "vatType": fiken_client.VAT_TYPE_25_PCT,
        }
        if line.product_id is not None:
            payload["productId"] = line.product_id
        else:
            # Free-text line: Fiken REQUIRES incomeAccount, so send the
            # default (3020 = services 25% VAT). Operator can change the
            # account on the line in Fiken's UI, or add the missing
            # Kategori product to Fiken so future drafts go through the
            # product-linked path.
            payload["incomeAccount"] = FIKEN_FREE_TEXT_INCOME_ACCOUNT
        if line.discount_percent > 0:
            payload["discount"] = line.discount_percent
        line_payloads.append(payload)

    # 6. POST the draft.
    issue_date = datetime.now(UTC).strftime("%Y-%m-%d")
    # Draft-level "Kommentar" (Fiken UI) → `invoiceText` field. Picked
    # per invoice_type so oppstart and slutt show a different default
    # note on the printed invoice. Both texts are editable via env vars
    # (settings.fiken_invoice_text_{oppstart,slutt}); empty string in
    # either → engine omits the field and Fiken falls back to the
    # company-level default ("endre standard" in the UI).
    invoice_text = (
        settings.fiken_invoice_text_oppstart
        if invoice_type == "oppstart"
        else settings.fiken_invoice_text_slutt
    ).strip() or None

    # Auto-pick the company's bank account number so the printed invoice
    # shows a Kontonummer (Fiken UI: "Kontonummer endre standard") instead
    # of the blank placeholder. None → omit the field; Fiken uses its own
    # default. Best-effort: log + continue when resolution fails.
    bank_account_number = await _ensure_bank_account_number(company_slug)

    try:
        draft = await fiken_client.create_invoice_draft(
            company_slug,
            customer_id=customer_id,
            issue_date=issue_date,
            days_until_due_date=DEFAULT_DAYS_UNTIL_DUE_DATE,
            reference=project_title or project_page_id,
            lines=line_payloads,
            your_reference=your_reference,
            invoice_text=invoice_text,
            bank_account_number=bank_account_number,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "send_faktura: draft POST failed for %s", project_page_id
        )
        result.action = "failed"
        result.note = f"Fiken create_invoice_draft: {err}"
        return result

    draft_id = (
        str(draft.get("draftId") or draft.get("id") or "")
        or (draft.get("Location") or "").rsplit("/", 1)[-1]
    )
    if not draft_id:
        logger.warning(
            "send_faktura: Fiken accepted the draft but the response "
            "carries no draftId/id: %s",
            draft,
        )
    result.fiken_invoice_id = draft_id or None

    # The POST response carries only the numeric draftId, but Fiken's UI
    # paths the draft on its `uuid`. Fetch once to read the uuid; degrade
    # to None on failure (the audit trail still preserves the draft_id).
    draft_url: str | None = None
    if draft_id:
        try:
            full_draft = await fiken_client.get_invoice_draft(
                company_slug, draft_id
            )
            uuid = full_draft.get("uuid")
            if uuid:
                draft_url = (
                    f"https://fiken.no/foretak/{company_slug}/fakturautkast/{uuid}"
                )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura: GET draft %s for uuid failed: %s",
                draft_id,
                err,
            )
    result.draft_url = draft_url

    # 7. Audit trail: persist FikenInvoice + FikenInvoiceLine.
    if draft_id:
        async with SessionLocal() as session:
            invoice_fraction = (
                OPPSTART_FRACTION if invoice_type == "oppstart" else 1.0
            )
            await session.execute(
                pg_insert(FikenInvoice)
                .values(
                    company_slug=company_slug,
                    fiken_invoice_id=draft_id,
                    project_page_id=project_page_id,
                    invoice_type=invoice_type,
                    invoice_fraction=invoice_fraction,
                )
                .on_conflict_do_update(
                    index_elements=["company_slug", "fiken_invoice_id"],
                    set_={
                        "project_page_id": project_page_id,
                        "invoice_type": invoice_type,
                        "invoice_fraction": invoice_fraction,
                    },
                )
            )
            for line in lines:
                if not line.oppgave_page_id:
                    continue
                await session.execute(
                    pg_insert(FikenInvoiceLine)
                    .values(
                        company_slug=company_slug,
                        fiken_invoice_id=draft_id,
                        oppgave_page_id=line.oppgave_page_id,
                        discipline=line.discipline,
                        billed_amount_ore=line.amount_nok_ore,
                        unit_price_ore=line.amount_nok_ore,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            "company_slug",
                            "fiken_invoice_id",
                            "oppgave_page_id",
                        ]
                    )
                )
            await session.commit()

    # 8. Stamp each billed Oppgave + Project + draft URL.
    # The audit trail (step 7) ALREADY persisted what was billed, so a
    # failure here is a "Notion is out of sync but Fiken + Postgres
    # agree" condition the operator can repair by hand.
    new_oppgave_status = (
        FAKTURERT_STATUS_50 if invoice_type == "oppstart" else FAKTURERT_STATUS_FULL
    )
    for line in lines:
        if not line.oppgave_page_id:
            continue
        try:
            await notion_client.set_oppgave_billed(
                line.oppgave_page_id,
                status=new_oppgave_status,
                billed_amount_nok=line.new_billed_amount_nok,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura: failed to stamp Oppgave %s: %s",
                line.oppgave_page_id,
                err,
            )

    new_project_status = (
        FAKTURA_STATUS_OPPSTART_DONE
        if invoice_type == "oppstart"
        else FAKTURA_STATUS_FULL_DONE
    )
    try:
        await notion_client.set_project_faktura_status(
            project_page_id, status=new_project_status
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "send_faktura: failed to flip project %s Faktura status to %r: %s",
            project_page_id,
            new_project_status,
            err,
        )

    if draft_url:
        try:
            await notion_client.set_project_draft_url(
                project_page_id, url=draft_url
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura: failed to write draft URL on %s: %s",
                project_page_id,
                err,
            )

    # Brreg writeback: now that the draft is created and Notion stamping
    # is done, propagate Brreg's official record onto the Notion
    # Fakturamottaker so the data converges over time. Best-effort —
    # already-correct rows are no-ops, failures log + continue. Only
    # runs when we actually got a Brreg payload (don't overwrite
    # operator data with a fallback name).
    if brreg_payload is not None and orgnr:
        brreg_navn = (brreg_payload.get("navn") or "").strip()
        if brreg_navn:
            try:
                await _update_fakturamottaker_in_notion(
                    project_page, orgnr, brreg_navn
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    "send_faktura: Brreg writeback to Notion failed for "
                    "%s: %s (non-fatal)",
                    project_page_id,
                    err,
                )

    logger.info(
        "✅ send_faktura: %s draft for %r — %d line(s), customer=%s, draft_id=%s",
        invoice_type,
        project_title or project_page_id,
        len(lines),
        result.customer_match,
        draft_id or "<unknown>",
    )
    return result
