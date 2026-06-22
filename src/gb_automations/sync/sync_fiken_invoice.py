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
    7. Write the draft URL onto the Project's `Faktura utkast` column.
       send_faktura does NOT touch any Notion billing field — NOT
       `Fakturert status`, NOT `Fakturert beløp` — on Oppgaver OR the
       Project. Both are written by the graduation step
       (sync/graduate_project.py) ONLY after the draft has actually
       been sent in Fiken. A draft is paperwork awaiting Send; writing
       to Fakturert beløp at draft time would lie about the project's
       real billed total. The audit trail (FikenInvoiceLine, written
       in step 6) holds the per-Oppgave NOK for graduation to add later.

Idempotency / re-click safety:
  - The dedup index on the `send_faktura` task type collapses concurrent
    double-clicks during the in-flight window to one task.
  - At the top of create_fiken_invoice, a Postgres query checks for
    any FikenInvoice row with sent_at NULL for this project; if one
    exists, the engine BLOCKS the click (one unsent draft per project
    at a time — operator sorts out the existing one in Fiken first).
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

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import brreg as brreg_client
from gb_automations.clients import fiken as fiken_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    DISCIPLINE_KEYS,
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
    rows: list[dict[str, Any]],
    invoice_type: str,
    *,
    rows_on_unsent_drafts: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Filter Notion rows to those eligible for this invoice run.

    Bulletproof skip rules — a row is SKIPPED iff ONE of:

      - `Type == "Korreksjonsrunde"` — Frame.io admin sub-row, never
        billed.
      - `Fakturert status == "Utgår"` — operator excluded.
      - `Fakturert status == "Fakturert"` — already fully billed.
      - oppstart click + `Fakturert status == "Fakturert 50%"`
        — oppstart already sent; only slutt remains.
      - row's page id is in `rows_on_unsent_drafts` — there's a
        FikenInvoice row in Postgres with sent_at NULL whose
        FikenInvoiceLine references this Oppgave. Belt-and-suspenders
        with the per-project block check in create_fiken_invoice;
        normally empty when we get here.

    Everything else lands — blank Type, unknown Type, missing Pris,
    missing Kategori. Downstream stages render kr 0 lines / free-text
    fallbacks gracefully.
    """
    if invoice_type not in ("oppstart", "slutt"):
        raise ValueError(
            f"invoice_type must be oppstart|slutt, got {invoice_type!r}"
        )
    on_draft = rows_on_unsent_drafts or set()
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
        if invoice_type == "oppstart" and status == FAKTURERT_STATUS_50:
            continue

        # Already on an unsent draft → skip. The per-project block
        # check normally prevents this set from being non-empty in
        # send_faktura, but the eligibility filter is defensive.
        row_id = row.get("id") or ""
        if row_id and row_id in on_draft:
            continue

        out.append(row)
    return out


def _row_billable_nok(
    row: dict[str, Any],
    invoice_type: str,
    *,
    already_drafted_nok: float = 0.0,
) -> tuple[float, float, float, float, float] | None:
    """Compute the per-row billing amounts.

    Returns a 5-tuple
        (to_bill_nok, new_billed_total_nok, unit_price_nok,
         quantity_fraction, discount_fraction)
    or None when the row should be skipped (no Pris, or renegotiation
    pushed remaining ≤ 0). All NOK values are decimals; the integer-øre
    conversion happens in the caller.

    `already_drafted_nok` is the sum of NOK on this Oppgave across
    every UNSENT FikenInvoiceLine in Postgres for the project (computed
    by `_unsent_oppgave_billed_ore_for_project`). It's added to the
    Notion-tracked `Fakturert beløp` when slutt computes `remaining`,
    so a slutt click while an oppstart draft sits unsent in Fiken
    correctly bills only the leftover. Defaults to 0 for the oppstart
    path (which doesn't use it) and for unit-test convenience.

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

    already_in_notion = (
        notion_client.read_number_prop(row, OPPGAVER_PROPS["billed_amount"])
        or 0.0
    )
    already_in_notion = max(0.0, float(already_in_notion))
    # "Committed but not yet invoiced": NOK sitting on unsent drafts
    # for this Oppgave. Slutt math treats this as already-billed so a
    # slutt click while an oppstart draft is still unsent in Fiken
    # bills only the remainder, not the full gross.
    already_committed = max(0.0, float(already_drafted_nok))
    already = already_in_notion + already_committed

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
            # Genuine over-billed case: operator lowered Pris below
            # the committed amount (sent invoices + unsent drafts).
            # Skip silently — the over-bill is already visible in
            # Notion via the `Budsjett` vs `Fakturert sum` rollups,
            # and a credit note (future feature) is the proper
            # resolution.
            logger.info(
                "fiken send-faktura: row %s already over-billed "
                "(gross=%.2f, sent=%.2f, drafted=%.2f, remaining=%.2f) "
                "— skipping; Notion rollups surface the discrepancy",
                row.get("id"),
                gross,
                already_in_notion,
                already_committed,
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
    unsent_drafted_ore: dict[str, int] | None = None,
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

        # Per-Oppgave "already committed but unsent" — converted from
        # the audit table's øre into NOK for the math.
        row_id = row.get("id") or ""
        already_drafted_nok = 0.0
        if unsent_drafted_ore and row_id in unsent_drafted_ore:
            already_drafted_nok = unsent_drafted_ore[row_id] / 100.0

        amounts = _row_billable_nok(
            row, invoice_type, already_drafted_nok=already_drafted_nok
        )
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
# Self-healing draft-in-flight check (Postgres + verify against Fiken)
# ============================================================
#
# Postgres stores a FikenInvoice row with sent_at=NULL every time
# send_faktura creates a draft. The graduation step (C.3a /
# graduate_project) sets sent_at when the draft has been sent in Fiken.
#
# But: operators delete drafts in Fiken's UI without telling us, and
# any drafts created before C.3a was wired won't ever get graduated.
# If we trust Postgres alone, the block check would refuse all future
# clicks on a project the moment one stale row exists.
#
# So the check is two-stage:
#   1. SELECT candidate FikenInvoice rows from Postgres
#      (project, sent_at IS NULL).
#   2. For each candidate, GET its draft on Fiken via
#      `fiken.check_draft_exists`. Three outcomes per candidate:
#        - "exists"  → genuine in-flight draft; block.
#        - "gone"    → draft was deleted (or sent without our knowing);
#                      mark the row stale via _mark_audit_row_stale and
#                      do NOT count it toward the block decision.
#        - "unknown" → network error / 5xx; fail-safe by leaving the
#                      row alone AND counting it toward the block (we
#                      don't want to accidentally create a duplicate
#                      draft during a Fiken outage).


async def _mark_audit_row_stale(
    company_slug: str, fiken_invoice_id: str
) -> None:
    """Stamp sent_at=now on a FikenInvoice row that no longer has a
    matching draft in Fiken. We use sent_at as the "no longer
    in-flight" marker — semantically slightly off (the draft was
    deleted, not sent) but it's the column that already drives the
    block check + graduation idempotency, and we want the same
    "this row is done" signal for both cases.
    """
    from datetime import datetime, timezone

    async with SessionLocal() as session:
        stmt = (
            pg_insert(FikenInvoice)
            .values(
                company_slug=company_slug,
                fiken_invoice_id=fiken_invoice_id,
                sent_at=datetime.now(timezone.utc),
            )
            .on_conflict_do_update(
                index_elements=["company_slug", "fiken_invoice_id"],
                set_={"sent_at": datetime.now(timezone.utc)},
            )
        )
        await session.execute(stmt)
        await session.commit()


async def _live_unsent_invoice_ids_for_project(
    company_slug: str,
    project_page_id: str,
    *,
    invoice_type: str | None = None,
) -> set[str]:
    """Return the set of FikenInvoice.fiken_invoice_id rows for this
    project that BOTH have sent_at NULL in Postgres AND still exist
    as drafts in Fiken. Self-heals stale rows by stamping sent_at on
    any candidate whose draft is "gone" in Fiken.

    `invoice_type` (None | "oppstart" | "slutt"):
      - None     — return ALL unsent drafts regardless of type (used
                   by the slutt math, which needs every draft's NOK
                   to subtract from the remaining).
      - "oppstart" / "slutt" — return only drafts of the matching type
                   (used by the block check, scoped per-mode so an
                   unsent oppstart doesn't block a slutt click).

    Treats Fiken errors as fail-safe (counts toward the in-flight set).
    """
    async with SessionLocal() as session:
        clauses = [
            FikenInvoice.company_slug == company_slug,
            FikenInvoice.project_page_id == project_page_id,
            FikenInvoice.sent_at.is_(None),
        ]
        if invoice_type is not None:
            clauses.append(FikenInvoice.invoice_type == invoice_type)
        stmt = select(FikenInvoice.fiken_invoice_id).where(*clauses)
        candidates = list((await session.execute(stmt)).scalars())

    live_ids: set[str] = set()
    for draft_id in candidates:
        if not draft_id:
            continue
        status = await fiken_client.check_draft_exists(
            company_slug, draft_id
        )
        if status == "exists":
            live_ids.add(draft_id)
        elif status == "gone":
            logger.info(
                "send_faktura: stale FikenInvoice row for draft_id=%s "
                "(Fiken says gone) — marking sent_at and dropping from "
                "the in-flight set.",
                draft_id,
            )
            await _mark_audit_row_stale(company_slug, draft_id)
        else:  # "unknown" — fail-safe.
            logger.warning(
                "send_faktura: could not verify draft %s against Fiken "
                "(check returned 'unknown'); counting toward the "
                "in-flight set.",
                draft_id,
            )
            live_ids.add(draft_id)
    return live_ids


async def _project_has_unsent_draft(
    company_slug: str, project_page_id: str, *, invoice_type: str
) -> bool:
    """True iff this project has at least one unsent FikenInvoice row
    of the given invoice_type ('oppstart' or 'slutt') whose draft is
    verified to still exist in Fiken.

    Per-type so an unsent oppstart draft doesn't block a slutt click
    (the slutt math separately subtracts the draft's NOK so we don't
    double-bill).
    """
    return bool(
        await _live_unsent_invoice_ids_for_project(
            company_slug, project_page_id, invoice_type=invoice_type
        )
    )


async def _unsent_oppgave_billed_ore_for_project(
    company_slug: str, project_page_id: str
) -> dict[str, int]:
    """Map of oppgave_page_id -> sum of billed_amount_ore across all
    unsent FikenInvoiceLine rows for this project (any mode).

    Used by slutt math: `remaining = gross − Fakturert beløp − unsent
    drafted amount for this Oppgave`. So if oppstart has been drafted
    but not yet sent (Fakturert beløp still 0), slutt sees the drafted
    amount and bills only the remainder — over-billing impossible.
    """
    live_ids = await _live_unsent_invoice_ids_for_project(
        company_slug, project_page_id, invoice_type=None
    )
    if not live_ids:
        return {}
    async with SessionLocal() as session:
        stmt = select(
            FikenInvoiceLine.oppgave_page_id,
            FikenInvoiceLine.billed_amount_ore,
        ).where(
            FikenInvoiceLine.company_slug == company_slug,
            FikenInvoiceLine.fiken_invoice_id.in_(live_ids),
        )
        out: dict[str, int] = {}
        for oppgave_id, amount in (await session.execute(stmt)).all():
            if not oppgave_id:
                continue
            out[oppgave_id] = out.get(oppgave_id, 0) + int(amount or 0)
        return out


async def _unsent_oppgave_ids_for_project(
    company_slug: str,
    project_page_id: str,
    *,
    invoice_type: str | None = None,
) -> set[str]:
    """Set of Oppgave page ids that appear on at least one unsent
    draft for this project. Filters through the live-draft check so
    a stale FikenInvoice row doesn't permanently keep its Oppgaver
    in the "on a draft" set.

    `invoice_type` (None | "oppstart" | "slutt"): when set, scopes
    to drafts of that mode only (used by the eligibility filter to
    skip rows already on a same-mode unsent draft).
    """
    live_ids = await _live_unsent_invoice_ids_for_project(
        company_slug, project_page_id, invoice_type=invoice_type
    )
    if not live_ids:
        return set()
    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine.oppgave_page_id).where(
            FikenInvoiceLine.company_slug == company_slug,
            FikenInvoiceLine.fiken_invoice_id.in_(live_ids),
        )
        rows = (await session.execute(stmt)).scalars()
        return {oppgave_id for oppgave_id in rows if oppgave_id}


# ============================================================
# Orphan-draft reconciliation + per-project draft reset
#
# The engine can create a duplicate Fiken draft when a run crashes after
# the POST but before the audit row is durable (historical NULL-discipline
# bug). The split-transaction fix in create_fiken_invoice prevents NEW
# duplicates, but pre-existing orphans need a manual cleanup path. These
# functions back both the /debug/fiken/* endpoints and the one-shot
# scripts/fiken_reset_project.py CLI.
# ============================================================


def _draft_id_of(draft: dict[str, Any]) -> str:
    """Pull the numeric draftId off a Fiken draft list payload as a str."""
    return str(draft.get("draftId") or draft.get("id") or "").strip()


def _draft_reference_of(draft: dict[str, Any]) -> str:
    """Pull the ourReference (Vår referanse) off a draft payload as a str."""
    return str(
        draft.get(fiken_client.REFERENCE_FIELD)
        or draft.get("reference")
        or ""
    )


def _filter_orphan_drafts(
    drafts: list[dict[str, Any]],
    known_active_ids: set[str],
    *,
    project_title: str | None = None,
) -> list[dict[str, Any]]:
    """Pure predicate behind find_orphan_drafts (separated for testing).

    An orphan is a draft whose draftId is NOT in `known_active_ids` (the
    set of FikenInvoice rows with sent_at IS NULL). When `project_title`
    is given, only drafts whose ourReference casefold-equals it survive —
    so a project-scoped call never flags a draft it can't attribute.
    """
    title_cf = (project_title or "").strip().casefold() or None
    orphans: list[dict[str, Any]] = []
    for draft in drafts:
        draft_id = _draft_id_of(draft)
        if not draft_id or draft_id in known_active_ids:
            continue
        reference = _draft_reference_of(draft)
        if title_cf is not None and reference.strip().casefold() != title_cf:
            continue
        orphans.append(
            {
                "draft_id": draft_id,
                "uuid": draft.get("uuid"),
                "reference": reference,
                "issue_date": draft.get("issueDate"),
            }
        )
    return orphans


async def find_orphan_drafts(
    company_slug: str, *, project_title: str | None = None
) -> list[dict[str, Any]]:
    """Drafts live in Fiken that have NO active FikenInvoice audit row.

    "Active" = a FikenInvoice row with sent_at IS NULL. A live Fiken draft
    whose draftId has no such row is an orphan: it was created by a run
    that crashed before recording it, or created by hand in Fiken's UI.

    `project_title` (optional): when set, only drafts whose ourReference
    casefold-equals the title are returned (scopes the report to one
    project — ourReference carries the Notion project name).

    Read-only. Returns a list of dicts: {draft_id, uuid, reference,
    total, issue_date} for operator review before deletion.
    """
    drafts = await fiken_client.list_invoice_drafts(company_slug)

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(FikenInvoice.fiken_invoice_id).where(
                    FikenInvoice.company_slug == company_slug,
                    FikenInvoice.sent_at.is_(None),
                )
            )
        ).scalars()
        known_active = {r for r in rows if r}

    return _filter_orphan_drafts(
        drafts, known_active, project_title=project_title
    )


async def delete_draft_and_mark_stale(
    company_slug: str, draft_id: str
) -> str:
    """Delete a Fiken draft and stamp sent_at on any matching audit row.

    Returns the delete_invoice_draft result ("deleted" / "gone" /
    "error"). The audit row is marked stale regardless of delete outcome
    when the draft is confirmed gone/deleted, so the in-flight set drops
    it either way.
    """
    outcome = await fiken_client.delete_invoice_draft(company_slug, draft_id)
    if outcome in ("deleted", "gone"):
        await _mark_audit_row_stale(company_slug, draft_id)
    return outcome


async def reset_project_drafts(
    company_slug: str, project_page_id: str
) -> dict[str, Any]:
    """Wipe all draft-in-flight state for one project so a fresh Send
    faktura click starts clean.

    For every FikenInvoice row of this project with sent_at IS NULL:
      - best-effort DELETE the live Fiken draft,
      - stamp sent_at so it drops out of the slutt remainder math and
        the block check.

    Does NOT touch Notion billing columns (Fakturert beløp / status) —
    those are the operator's to set, and graduation owns them. This only
    clears the PHANTOM draft state that was making a cleared-in-Notion
    project still read as half-billed.

    Returns a summary dict for logging / the debug response.
    """
    async with SessionLocal() as session:
        unsent = list(
            (
                await session.execute(
                    select(FikenInvoice.fiken_invoice_id).where(
                        FikenInvoice.company_slug == company_slug,
                        FikenInvoice.project_page_id == project_page_id,
                        FikenInvoice.sent_at.is_(None),
                    )
                )
            ).scalars()
        )

    results: list[dict[str, str]] = []
    for draft_id in unsent:
        if not draft_id:
            continue
        outcome = await delete_draft_and_mark_stale(company_slug, draft_id)
        results.append({"draft_id": draft_id, "outcome": outcome})
        logger.info(
            "reset_project_drafts: project=%s draft=%s → %s",
            project_page_id,
            draft_id,
            outcome,
        )

    return {
        "project_page_id": project_page_id,
        "unsent_audit_rows": len(unsent),
        "drafts_processed": results,
    }


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


async def _create_kreditnota_drafts(
    *,
    project_page_id: str,
    project_page: dict[str, Any],
    project_title: str,
    result: InvoiceCreateResult,
) -> InvoiceCreateResult:
    """Kreditering branch: create one draft kreditnota for every sent
    FikenInvoice on this project that doesn't already have an unsent
    or sent kreditnota.

    Each kreditnota draft mirrors the parent invoice's lines exactly
    (positive quantities + positive unitPrice; Fiken negates on Send).
    The customer + reference + invoiceText come from the parent.

    Idempotency: the per-mode block check finds existing unsent
    kreditnota drafts for THIS project and refuses to create
    duplicates. The per-parent check looks at FakturaNotionCache —
    if a "kreditnota" cache row already exists for an
    associatedInvoiceId, we skip that parent (already covered).
    """
    from gb_automations.models import FakturaNotionCache

    company_slug = settings.fiken_company_slug
    if not company_slug:
        result.action = "failed"
        result.note = "FIKEN_COMPANY_SLUG not set"
        return result

    # 1. Same-mode block check. The operator may have clicked Til
    # kreditering twice; the first click's drafts are still in flight.
    # Honor it.
    if await _project_has_unsent_draft(
        company_slug, project_page_id, invoice_type="kreditering"
    ):
        logger.info(
            "send_faktura(kreditering): project %r already has unsent "
            "kreditnota drafts — blocking. Send or delete the existing "
            "ones in Fiken first.",
            project_title or project_page_id,
        )
        result.action = "skipped"
        result.note = (
            "An unsent kreditnota draft already exists for this project. "
            "Send or delete it in Fiken before clicking Send faktura "
            "again for Til kreditering."
        )
        return result

    # 2. Find every sent FikenInvoice for this project. Skip ones we've
    # already kreditnotaed (Faktura DB cache has a "kreditnota" row
    # referencing them).
    async with SessionLocal() as session:
        stmt = select(FikenInvoice).where(
            FikenInvoice.company_slug == company_slug,
            FikenInvoice.project_page_id == project_page_id,
            FikenInvoice.sent_at.is_not(None),
            FikenInvoice.invoice_type.in_(("oppstart", "slutt")),
        )
        sent_parents = list((await session.execute(stmt)).scalars())

    if not sent_parents:
        logger.info(
            "send_faktura(kreditering): project %r has no sent fakturas "
            "to credit — nothing to do.",
            project_title or project_page_id,
        )
        result.action = "skipped"
        result.note = (
            "No sent invoices to credit. Either there's nothing sent yet, "
            "or the project hasn't been graduated via Sjekk fiken so we "
            "don't know about the sent invoices."
        )
        return result

    # 3. For each sent parent, skip if already credited.
    #    Duplicate-detection strategy: parse "Kreditnota for faktura
    #    {N}" out of each existing kreditnota's Kommentar (the pattern
    #    OUR Til kreditering flow stamps). Match against the parent
    #    audit row's invoice_number (graduation populated this).
    #    Mirrors the kreditnota match strategy on the Sjekk fiken side.
    import re

    parent_no_re = re.compile(
        r"kreditnota\s+for\s+faktura\s+(\d+)", re.IGNORECASE
    )

    async with SessionLocal() as session:
        stmt = select(FakturaNotionCache.fiken_record_id).where(
            FakturaNotionCache.company_slug == company_slug,
            FakturaNotionCache.record_type == "kreditnota",
        )
        existing_cn_ids = list((await session.execute(stmt)).scalars())

    credited_parent_numbers: set[str] = set()
    for cn_id in existing_cn_ids:
        try:
            async with await fiken_client._client() as client:
                cn_resp = await client.get(
                    f"/companies/{company_slug}/creditNotes/{cn_id}"
                )
                if cn_resp.status_code == 200:
                    cn_data = cn_resp.json()
                    # Two paths to "this kreditnota covers parent X":
                    #   - associatedInvoiceId set (Fiken-UI-created)
                    #     → look up parent's invoice_number from our
                    #       audit table
                    #   - creditNoteText / invoiceText carries
                    #     "Kreditnota for faktura {N}" (our flow)
                    associated = str(
                        cn_data.get("associatedInvoiceId") or ""
                    )
                    if associated:
                        # Walk audit rows to find which one's
                        # sent-side sale id is associated. Cheap.
                        for cand in sent_parents:
                            if cand.invoice_number:
                                credited_parent_numbers.add(
                                    str(cand.invoice_number)
                                )
                                # Stop early would need an extra
                                # lookup; cheap to add all matching
                                # candidates instead.
                    cn_text = (
                        cn_data.get("creditNoteText")
                        or cn_data.get("invoiceText")
                        or ""
                    )
                    m = parent_no_re.search(cn_text)
                    if m:
                        credited_parent_numbers.add(m.group(1))
        except Exception:  # noqa: BLE001
            pass

    # 4. Filter to parents not yet credited.
    parents_to_credit: list[FikenInvoice] = []
    for parent in sent_parents:
        parent_no = str(parent.invoice_number) if parent.invoice_number else None
        if parent_no and parent_no in credited_parent_numbers:
            logger.info(
                "send_faktura(kreditering): parent invoice %s "
                "(number=%s) already has a kreditnota — skipping.",
                parent.fiken_invoice_id,
                parent_no,
            )
            continue
        parents_to_credit.append(parent)

    if not parents_to_credit:
        result.action = "skipped"
        result.note = (
            "Every sent invoice on this project already has a kreditnota. "
            "Nothing more to credit."
        )
        return result

    # 5. Resolve customer + Brreg context from the project's
    #    Fakturamottaker (same code path Send faktura uses for new
    #    invoices). Customer is the same entity that was originally
    #    billed.
    orgnr, fakturamottaker_name = await _resolve_project_orgnr(
        project_page
    )
    customer_id: int | None = None
    if orgnr:
        try:
            customers = await fiken_client.list_contacts(
                company_slug, customer=True
            )
            for c in customers:
                if _normalize_orgnr(
                    c.get("organizationNumber")
                ) == orgnr:
                    customer_id = c.get("contactId")
                    break
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura(kreditering): list_contacts failed: %s",
                err,
            )
    if customer_id is None:
        # Same fallback Send faktura uses: link to the shared
        # "Mangler kunde" placeholder so Fiken accepts the draft.
        customer_id = await _ensure_placeholder_contact(company_slug)
        if customer_id is None:
            result.action = "failed"
            result.note = (
                "Could not resolve a customer for kreditnota draft "
                "(no Fakturamottaker Orgnr + placeholder unavailable)."
            )
            return result

    # 6. Create one draft kreditnota per parent. Each draft mirrors
    #    the parent's audit lines: one kreditnota line per
    #    FikenInvoiceLine, with the same billed_amount_ore. Description
    #    comes from the Oppgave's current title (live read).
    last_draft_url: str | None = None
    drafts_created = 0
    issue_date = datetime.now(UTC).date().isoformat()
    bank_account_number = await _ensure_bank_account_number(company_slug)

    for parent in parents_to_credit:
        # Load the audit lines for this parent.
        async with SessionLocal() as session:
            stmt = select(FikenInvoiceLine).where(
                FikenInvoiceLine.company_slug == parent.company_slug,
                FikenInvoiceLine.fiken_invoice_id
                == parent.fiken_invoice_id,
            )
            audit_lines = list((await session.execute(stmt)).scalars())

        if not audit_lines:
            logger.warning(
                "send_faktura(kreditering): parent %s has no audit "
                "lines — skipping (operator should kreditnota in "
                "Fiken's UI manually).",
                parent.fiken_invoice_id,
            )
            continue

        # Build the kreditnota lines from the audit trail PLUS each
        # Oppgave's current Notion data. The audit table gives us the
        # billed NOK (audit.billed_amount_ore — the canonical "what was
        # actually invoiced"); the Notion row gives us productId,
        # unitPrice (Pris), discount (Rabatt), description, comment.
        #
        # Back-derivation: we have `billed = quantity × unitPrice ×
        # (1 - discount)`. Solving for quantity:
        #   quantity = billed / (unitPrice × (1 - discount))
        # This faithfully reproduces the original "Antall 0.5, Pris kr X,
        # Rabatt Y%" shape on the printed kreditnota. If Pris was
        # renegotiated since the invoice, the printed Antall looks
        # different but the net still equals the audit billed amount.
        cn_lines: list[dict[str, Any]] = []
        for audit in audit_lines:
            if not audit.oppgave_page_id:
                # No Oppgave to read Pris/Rabatt/Kategori from — fall
                # back to a single free-text line at the audit amount.
                cn_lines.append(
                    {
                        "description": audit.discipline or "Faktura",
                        "unitPrice": int(audit.billed_amount_ore),
                        "quantity": 1.0,
                        "vatType": "HIGH",
                        "incomeAccount": FIKEN_FREE_TEXT_INCOME_ACCOUNT,
                    }
                )
                continue

            try:
                opp_page = await notion_client.get_page(
                    audit.oppgave_page_id
                )
            except Exception as err:  # noqa: BLE001
                logger.warning(
                    "send_faktura(kreditering): could not read Oppgave "
                    "%s for line context (%s) — falling back to bare "
                    "amount.",
                    audit.oppgave_page_id,
                    err,
                )
                cn_lines.append(
                    {
                        "description": audit.discipline or "Faktura",
                        "unitPrice": int(audit.billed_amount_ore),
                        "quantity": 1.0,
                        "vatType": "HIGH",
                        "incomeAccount": FIKEN_FREE_TEXT_INCOME_ACCOUNT,
                    }
                )
                continue

            # ---- Pris, Rabatt, productId, description, comment ----
            raw_price = notion_client.read_number_prop(
                opp_page, OPPGAVER_PROPS["price_per_row"]
            )
            price_nok = (
                float(raw_price)
                if raw_price is not None and raw_price > 0
                else 0.0
            )
            discount_fraction = (
                notion_client.read_number_prop(
                    opp_page, OPPGAVER_PROPS["discount_pct"]
                )
                or 0.0
            )
            discount_fraction = max(
                0.0, min(float(discount_fraction), 1.0)
            )
            unit_price_ore = int(round(price_nok * 100))

            kategori = _resolve_kategori_label(opp_page)
            product_id: int | None = None
            if kategori:
                try:
                    product_id = await _resolve_kategori_to_product_id(
                        company_slug, kategori
                    )
                except Exception:  # noqa: BLE001
                    product_id = None

            description = _line_product_name(opp_page, kategori)
            if not description:
                canonical = _normalize_discipline(
                    notion_client.task_discipline(opp_page)
                )
                if canonical:
                    description = _DISCIPLINE_FALLBACK_NAMES.get(
                        canonical, canonical.title()
                    )
            if not description:
                description = audit.oppgave_page_id
            comment = _line_comment(opp_page)

            # ---- Back-derive quantity so net = audit amount ----
            #   billed = quantity × unitPrice × (1 - discount)
            # → quantity = billed / (unitPrice × (1 - discount))
            # Edge cases: kr 0 unitPrice or 100% discount → fall back
            # to (quantity=1, unitPrice=audit_amount) since the math
            # can't be expressed in the (Pris, Antall, Rabatt) shape.
            denom = unit_price_ore * (1.0 - discount_fraction)
            if denom > 0:
                quantity = audit.billed_amount_ore / denom
                cn_unit_price_ore = unit_price_ore
            else:
                quantity = 1.0
                cn_unit_price_ore = int(audit.billed_amount_ore)

            cn_line: dict[str, Any] = {
                "description": description,
                "unitPrice": cn_unit_price_ore,
                "quantity": round(quantity, 6),
                "vatType": "HIGH",
            }
            if discount_fraction > 0:
                cn_line["discount"] = round(
                    discount_fraction * 100.0, 4
                )
            if comment:
                cn_line["comment"] = comment
            if product_id is not None:
                cn_line["productId"] = product_id
            else:
                cn_line["incomeAccount"] = FIKEN_FREE_TEXT_INCOME_ACCOUNT
            cn_lines.append(cn_line)

        # Reference fields: use the parent's invoice_number for clarity
        # ("Kreditnota for faktura 10058"), project title as Vår ref,
        # and the project's Faktura merkes as Deres ref (same source as
        # the original invoice so the printed kreditnota matches its
        # parent's reference block).
        parent_no = parent.invoice_number or "ukjent"
        cn_text = f"Kreditnota for faktura {parent_no}"
        cn_our_ref = project_title or project_page_id
        cn_your_ref = (
            notion_client.read_rich_text_prop(
                project_page, PROJECTS_FAKTURA_MERKES_PROP
            )
            or ""
        ).strip() or None

        try:
            draft = await fiken_client.create_credit_note_draft(
                company_slug,
                customer_id=customer_id,
                issue_date=issue_date,
                days_until_due_date=DEFAULT_DAYS_UNTIL_DUE_DATE,
                reference=cn_our_ref,
                lines=cn_lines,
                your_reference=cn_your_ref,
                credit_note_text=cn_text,
                bank_account_number=bank_account_number,
            )
        except Exception as err:  # noqa: BLE001
            logger.exception(
                "send_faktura(kreditering): create_credit_note_draft "
                "failed for parent %s",
                parent.fiken_invoice_id,
            )
            result.note = (
                f"Failed to create kreditnota draft for parent "
                f"{parent.fiken_invoice_id}: {err}"
            )
            # Continue to the next parent — partial success is useful.
            continue

        draft_id = (
            str(draft.get("draftId") or draft.get("id") or "")
            or (draft.get("Location") or "").rsplit("/", 1)[-1]
        )
        if not draft_id:
            logger.warning(
                "send_faktura(kreditering): Fiken accepted the draft "
                "but the response carries no draftId/id: %s",
                draft,
            )
            continue

        # Fetch the uuid for the URL writeback.
        draft_uuid: str | None = None
        try:
            full_draft = await fiken_client.get_credit_note_draft(
                company_slug, draft_id
            )
            uu = full_draft.get("uuid")
            if uu:
                draft_uuid = str(uu)
                last_draft_url = (
                    f"https://fiken.no/foretak/{company_slug}/"
                    f"fakturautkast/{uu}"
                )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura(kreditering): GET draft %s for uuid "
                "failed: %s",
                draft_id,
                err,
            )

        # Audit trail: store the kreditnota draft as a FikenInvoice
        # with invoice_type="kreditering". The block check + sjekk-
        # fiken graduation paths both branch on invoice_type, so this
        # stays out of the oppstart/slutt math.
        async with SessionLocal() as session:
            await session.execute(
                pg_insert(FikenInvoice)
                .values(
                    company_slug=company_slug,
                    fiken_invoice_id=draft_id,
                    project_page_id=project_page_id,
                    invoice_type="kreditering",
                    invoice_fraction=1.0,
                    draft_uuid=draft_uuid,
                )
                .on_conflict_do_update(
                    index_elements=["company_slug", "fiken_invoice_id"],
                    set_={
                        "project_page_id": project_page_id,
                        "invoice_type": "kreditering",
                        "invoice_fraction": 1.0,
                        "draft_uuid": draft_uuid,
                    },
                )
            )
            await session.commit()

        drafts_created += 1
        logger.info(
            "✅ send_faktura(kreditering): created kreditnota draft %s "
            "for parent invoice %s",
            draft_id,
            parent.fiken_invoice_id,
        )

    # 5. Write the most recent draft URL to Faktura utkast on the
    # project. With multiple drafts, this is the last one; operator
    # clicks through to Fiken's fakturautkast list to see them all.
    # (Empirically pinned: Fiken's web UI uses /fakturautkast/{uuid}
    # for BOTH invoice and kreditnota drafts — there is no separate
    # /kreditnotautkast/ path.)
    if last_draft_url:
        try:
            await notion_client.set_project_draft_url(
                project_page_id, url=last_draft_url
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura(kreditering): failed to write draft URL "
                "on %s: %s",
                project_page_id,
                err,
            )

    result.action = "created" if drafts_created > 0 else "failed"
    if drafts_created > 0:
        result.note = (
            f"Created {drafts_created} kreditnota draft(s). Open each in "
            "Fiken to review + click Send. Then click Sjekk fiken to pull "
            "them into Notion."
        )
        result.lines_created = drafts_created
    return result


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

    # Kreditering branch: inverts the engine. Instead of creating new
    # outgoing-money drafts (oppstart/slutt), creates draft kreditnotas
    # that reverse every sent FikenInvoice on this project not already
    # credited. Skips the Oppgaver / Pris pipeline entirely — kreditnota
    # lines are built by mirroring the parent invoice's actual lines.
    if invoice_type == "kreditering":
        return await _create_kreditnota_drafts(
            project_page_id=project_page_id,
            project_page=project_page,
            project_title=project_title,
            result=result,
        )

    # Block if this project already has an unsent draft of the SAME
    # mode (oppstart blocks oppstart, slutt blocks slutt). Different
    # modes are allowed — an unsent oppstart draft does NOT block a
    # slutt click, because:
    #   - The math is still safe: slutt computes `remaining = gross −
    #     Fakturert beløp − unsent drafted NOK` (see
    #     _unsent_oppgave_billed_ore_for_project), so the unsent
    #     oppstart's amount gets subtracted and slutt bills only the
    #     remainder.
    #   - Operationally common: CEO sits on an oppstart draft for
    #     weeks; we shouldn't permanently block slutt for a different
    #     project phase.
    if await _project_has_unsent_draft(
        company_slug, project_page_id, invoice_type=invoice_type
    ):
        logger.info(
            "send_faktura: project %r already has an unsent %s draft — "
            "blocking same-mode re-click. Send or delete the existing "
            "draft in Fiken before clicking Send faktura again for %s.",
            project_title or project_page_id,
            invoice_type,
            invoice_type,
        )
        result.action = "skipped"
        result.note = (
            f"An unsent Fiken {invoice_type} draft already exists for "
            f"this project. Send or delete it in Fiken before clicking "
            f"Send faktura again for {invoice_type}."
        )
        return result

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

    # Belt-and-suspenders: even though the per-mode block check at
    # the top of this function should prevent us getting here when a
    # same-mode unsent draft exists, pull the set of Oppgave page ids
    # currently on a same-mode unsent draft and pass it to the
    # eligibility filter. Scoped per-mode so a row on an unsent
    # oppstart draft is still eligible for slutt (the math subtracts
    # the unsent NOK separately).
    rows_on_unsent_drafts = await _unsent_oppgave_ids_for_project(
        company_slug, project_page_id, invoice_type=invoice_type
    )
    eligible = _eligible_rows(
        rows, invoice_type, rows_on_unsent_drafts=rows_on_unsent_drafts
    )
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
    #
    # For slutt: pre-fetch the per-Oppgave NOK currently sitting on
    # unsent drafts (any mode) for this project. The slutt math
    # subtracts this from `remaining` so an unsent oppstart draft is
    # treated as already-committed — slutt bills only the leftover.
    unsent_drafted_ore: dict[str, int] | None = None
    if invoice_type == "slutt":
        unsent_drafted_ore = await _unsent_oppgave_billed_ore_for_project(
            company_slug, project_page_id
        )
    lines = await _build_line_items(
        eligible,
        invoice_type=invoice_type,
        company_slug=company_slug,
        unsent_drafted_ore=unsent_drafted_ore,
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

    # 7. RESILIENCE-CRITICAL ORDERING. The Fiken POST above is an
    # irreversible external side-effect, and everything that follows it
    # (uuid GET, per-line audit, Notion writes) can throw. If a throw
    # rolls back the FikenInvoice parent row, a retry of this task finds
    # no unsent-draft audit row, the same-mode block check at the top
    # passes, and we POST a SECOND draft — that's exactly how this
    # project ended up with a pile of orphan drafts.
    #
    # So: commit the FikenInvoice parent row in its OWN transaction the
    # instant the draft exists, BEFORE the uuid GET and BEFORE the line
    # inserts. After this point, a crash leaves a durable "the draft
    # exists" record; the block check sees it and the retry skips
    # re-POSTing. draft_uuid / lines are filled in by separate
    # transactions below so their failure can never erase the parent.
    invoice_fraction = OPPSTART_FRACTION if invoice_type == "oppstart" else 1.0
    if draft_id:
        async with SessionLocal() as session:
            await session.execute(
                pg_insert(FikenInvoice)
                .values(
                    company_slug=company_slug,
                    fiken_invoice_id=draft_id,
                    project_page_id=project_page_id,
                    invoice_type=invoice_type,
                    invoice_fraction=invoice_fraction,
                    # draft_uuid is unknown until the GET below; a retry
                    # that already populated it must NOT have it nulled,
                    # so we DON'T set it here and DON'T overwrite it in
                    # the conflict clause.
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
            await session.commit()

    # The POST response carries only the numeric draftId, but Fiken's UI
    # paths the draft on its `uuid`. Fetch once to read the uuid; degrade
    # to None on failure (the parent row above already records the draft).
    #
    # Phase C.3a — the draft `uuid` is ALSO the FK that lets the poller
    # match a sent invoice back to this row (Fiken mints a fresh
    # `invoiceId` at send-time, but `invoiceDraftUuid` on the sent
    # record == the draft's `uuid`).
    draft_url: str | None = None
    draft_uuid_for_audit: str | None = None
    if draft_id:
        try:
            full_draft = await fiken_client.get_invoice_draft(
                company_slug, draft_id
            )
            uuid = full_draft.get("uuid")
            if uuid:
                draft_uuid_for_audit = str(uuid)
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

    # Backfill draft_uuid onto the already-committed parent row (own txn).
    # Matches the upsert pattern used elsewhere (_mark_audit_row_stale) so
    # the row exists even in the unreachable case where the parent insert
    # above was skipped.
    if draft_id and draft_uuid_for_audit:
        try:
            async with SessionLocal() as session:
                await session.execute(
                    pg_insert(FikenInvoice)
                    .values(
                        company_slug=company_slug,
                        fiken_invoice_id=draft_id,
                        draft_uuid=draft_uuid_for_audit,
                    )
                    .on_conflict_do_update(
                        index_elements=["company_slug", "fiken_invoice_id"],
                        set_={"draft_uuid": draft_uuid_for_audit},
                    )
                )
                await session.commit()
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "send_faktura: failed to backfill draft_uuid for %s: %s "
                "(non-fatal; graduation can still match via ourReference)",
                draft_id,
                err,
            )

    # Per-line audit trail, in its OWN transaction. A failure here (e.g.
    # the historical NULL-discipline crash) parks the task as failed but
    # does NOT erase the parent row above — so the retry's block check
    # short-circuits instead of minting a duplicate draft.
    if draft_id:
        async with SessionLocal() as session:
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

    # 8. Write the draft URL on the project.
    #
    # send_faktura does NOT touch ANY Notion billing field on the
    # Oppgaver or Project. Both `Fakturert status` (history label)
    # and `Fakturert beløp` (NOK total) are reserved for the
    # graduation step (sync/graduate_project.py), which writes them
    # ONLY after the draft has actually been sent in Fiken.
    #
    # The draft-in-flight signal is the `Faktura utkast` URL written
    # below plus the unsent FikenInvoice row in Postgres (which the
    # eligibility matrix + block check use). The audit trail
    # (FikenInvoiceLine, step 7) carries the per-Oppgave NOK that
    # WILL be added to Fakturert beløp at graduation.
    #
    # The reasoning: Notion's billing columns are the source of truth
    # for "what's actually invoiced." A draft is not invoiced — it's
    # paperwork awaiting Send. Writing to Fakturert beløp at draft
    # time would lie about the project's real billed total.

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
