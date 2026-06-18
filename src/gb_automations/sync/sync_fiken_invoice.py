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
    FAKTURAMOTTAKER_PROPS,
    FIKEN_DEFAULT_INCOME_ACCOUNT,
    FIKEN_KATEGORI_TO_ACCOUNT,
    OPPGAVER_DESC_PROP,
    OPPGAVER_PROPS,
    PROJECTS_FAKTURA_MERKES_PROP,
    PROJECTS_FAKTURA_STATUS_PROP,
    PROJECTS_FAKTURAMOTTAKER_PROP,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import (
    FikenInvoice,
    FikenInvoiceLine,
    FikenPlaceholderContact,
    FikenProductCache,
)

logger = logging.getLogger(__name__)


# Per-discipline Fiken product number stems. Used as the stable Fiken-side
# productNumber (distinct from the numeric productId Fiken auto-assigns
# and which the cache stores). Free-text invoice lines don't actually link
# to a productId today (so the customer sees the row Navn instead of a
# generic "Interiør"), but the catalogue mirror keeps these registered so
# the price-by-discipline analytics in Fiken stay populated.
FIKEN_DISCIPLINE_PRODUCT_NUMBERS = {
    "interior": "goldbox-interior",
    "exterior": "goldbox-exterior",
    "animation": "goldbox-animation",
    "other": "goldbox-other",
}

# Display names used as the Fiken catalogue product name. Norwegian to
# match what the customer sees on the printed invoice; the canonical
# discipline keys are English for code consistency.
FIKEN_DISCIPLINE_DISPLAY_NAMES = {
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

    Two amount-shaped fields by design: `unit_price_nok_ore` is what we
    send to Fiken's `unitPrice` (un-discounted — Fiken applies `discount`
    on its side so the customer sees "Pris kr 5000 / Rabatt 15% / Sum
    kr 4250" on the printed invoice). `amount_nok_ore` is what was
    actually billed *after* Fiken applies the discount, used for the
    audit trail and the `Fakturert beløp` stampback in Notion.

    Fiken line text comes in two pieces by design:
      - `product_name` (→ Fiken `description`): the bold first line on
        the printed invoice. Sourced from the Oppgave's `Oppgave kategori`
        multi_select (first label) — e.g. "Næring - Interiør", "Print".
        Falls back to "Navn — Beskrivelse" when no Kategori is set.
      - `comment` (→ Fiken `comment`): the smaller sub-line below the
        product name. "Navn - Beskrivelse" (or just Navn when Beskrivelse
        is blank), so the customer reads "what category they're paying
        for" up top and "exactly which deliverable" right beneath.

    `income_account` is derived from the Kategori too via
    `FIKEN_KATEGORI_TO_ACCOUNT` (3020 for tjeneste, 3000 for vare, etc.).
    Defaults to 3020 with a WARN when Kategori is missing or unknown.
    """

    discipline: str            # canonical key, e.g. "interior"
    product_name: str          # → Fiken `description` (main bold line)
    comment: str               # → Fiken `comment` (sub-line)
    income_account: str        # → Fiken `incomeAccount` (kontonummer)
    unit_price_nok_ore: int    # full undiscounted price in øre → Fiken `unitPrice`
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

    A row passes iff:
      - `Type` is a recognized discipline (deliverable, not internal task).
      - `Fakturert status` is NOT one of {Fakturert, Utgår} — those are
        "skip, no matter what."
      - For oppstart: `Fakturert status == "Ikke fakturert"`.
      - For slutt:    `Fakturert status ∈ {"Ikke fakturert", "Fakturert 50%"}`.

    Blank `Fakturert status` (operator never set it) is treated as
    "Ikke fakturert" so the engine is forgiving on freshly-cloned templates.
    """
    if invoice_type not in ("oppstart", "slutt"):
        raise ValueError(
            f"invoice_type must be oppstart|slutt, got {invoice_type!r}"
        )
    out: list[dict[str, Any]] = []
    for row in rows:
        discipline = notion_client.task_discipline(row)
        if not _normalize_discipline(discipline):
            continue
        status = (
            notion_client.read_select_name(row, OPPGAVER_PROPS["billed_status"])
            or FAKTURERT_STATUS_IKKE
        )
        if status in (FAKTURERT_STATUS_FULL, FAKTURERT_STATUS_UTGAR):
            continue
        if invoice_type == "oppstart":
            if status != FAKTURERT_STATUS_IKKE:
                continue
        else:  # slutt
            if status not in (FAKTURERT_STATUS_IKKE, FAKTURERT_STATUS_50):
                continue
        out.append(row)
    return out


def _row_billable_nok(
    row: dict[str, Any], invoice_type: str
) -> tuple[float, float, float, float] | None:
    """Compute the per-row billing amounts.

    Returns a 4-tuple
        (to_bill_nok, new_billed_total_nok, unit_price_nok, discount_fraction)
    or None when the row should be skipped (no Pris, or renegotiation
    pushed remaining ≤ 0). All NOK values are decimals; the integer-øre
    conversion happens in the caller.

    Two ways to view "what's owed":
        post-rabatt:   gross = Pris × (1 − Rabatt)
        pre-rabatt:    unit_price = Pris (×  oppstart fraction when oppstart)

    The post-rabatt number drives the run amount and the Notion ledger
    (`Fakturert beløp`). The pre-rabatt number is the `unitPrice` we send
    to Fiken — Fiken applies the rabatt on its side so the customer sees
    "Pris kr X / Rabatt Y% / Sum kr Z" on the printed invoice rather
    than just a pre-discounted unit price.

    Math:
        gross         = Pris × (1 − Rabatt)        # Rabatt is a fraction (0.15 = 15%)
        already       = Fakturert beløp (running total Notion holds)
        remaining     = gross − already
        oppstart  →   to_bill    = round(gross × 0.5)        # 50% of fresh gross
                      unit_price = round(Pris × 0.5)         # 50% of fresh full price
        slutt     →   to_bill    = round(remaining)          # whatever is left
                      unit_price = round(remaining / (1 − Rabatt))
                                                              # pre-rabatt amount
                                                              # that yields the same
                                                              # remaining post-rabatt
        new_total     = already + to_bill

    Notes on the slutt unit_price derivation: at slutt time the row may
    have been partially billed at oppstart, so the "this run's pre-rabatt
    price" is whatever pre-rabatt amount Fiken would have to apply
    Rabatt% to in order to land at our intended `remaining`. That's the
    inverse: `unit_price = remaining / (1 − Rabatt)`. When Rabatt is 0
    this collapses to `unit_price = remaining`, matching the no-discount
    path.
    """
    price_nok = notion_client.read_number_prop(
        row, OPPGAVER_PROPS["price_per_row"]
    )
    if price_nok is None or price_nok <= 0:
        logger.warning(
            "fiken send-faktura: row %s has no Pris set — skipping",
            row.get("id"),
        )
        return None
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
    gross = float(price_nok) * (1.0 - discount_fraction)

    already = (
        notion_client.read_number_prop(row, OPPGAVER_PROPS["billed_amount"])
        or 0.0
    )
    already = max(0.0, float(already))

    if invoice_type == "oppstart":
        to_bill = round(gross * OPPSTART_FRACTION)
        unit_price = round(float(price_nok) * OPPSTART_FRACTION)
    else:
        to_bill = round(gross - already)
        # Pre-rabatt amount that yields `to_bill` post-rabatt. Rabatt=1.0
        # is impossible here (clamped, and gross would be 0 → already
        # caught), so the division is safe.
        unit_price = (
            round(to_bill / (1.0 - discount_fraction))
            if discount_fraction < 1.0
            else to_bill
        )
    if to_bill <= 0:
        logger.info(
            "fiken send-faktura: row %s has nothing left to bill "
            "(gross=%.2f, already=%.2f, to_bill=%.2f) — skipping",
            row.get("id"),
            gross,
            already,
            to_bill,
        )
        return None
    return (
        float(to_bill),
        already + float(to_bill),
        float(unit_price),
        discount_fraction,
    )


def _resolve_kategori_and_account(row: dict[str, Any]) -> tuple[str | None, str]:
    """Read `Oppgave kategori` (multi_select) and resolve to
    (kategori_label, income_account).

    Multi-select: a row CAN carry multiple kategoris, but a Fiken line
    has one description + one income account. The engine picks the FIRST
    selected label (Notion preserves the operator's selection order).
    Unknown / unmapped labels and missing kategori both fall back to
    `FIKEN_DEFAULT_INCOME_ACCOUNT` (3020) with a WARN log so the gap is
    visible.

    Returns (None, default_account) when no Kategori is set — caller
    then composes the line description from Navn/Beskrivelse instead.
    """
    labels = notion_client.read_multi_select_names(
        row, OPPGAVER_PROPS["kategori"]
    )
    if not labels:
        return None, FIKEN_DEFAULT_INCOME_ACCOUNT
    first = labels[0]
    account = FIKEN_KATEGORI_TO_ACCOUNT.get(first)
    if account is None:
        logger.warning(
            "fiken send-faktura: row %s has Oppgave kategori %r which is "
            "not in FIKEN_KATEGORI_TO_ACCOUNT — defaulting to account %s "
            "(add the kategori to the map in config.py to route it explicitly)",
            row.get("id"),
            first,
            FIKEN_DEFAULT_INCOME_ACCOUNT,
        )
        account = FIKEN_DEFAULT_INCOME_ACCOUNT
    return first, account


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
        return FIKEN_DISCIPLINE_DISPLAY_NAMES.get(canonical, canonical.title())
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


def _build_line_items(
    eligible: list[dict[str, Any]], *, invoice_type: str
) -> list[InvoiceLine]:
    """Compose Fiken invoice lines from eligible rows — ONE line per
    Oppgave. Per-line amount is the NOK already computed by
    `_row_billable_nok`. Free-text lines (no productId) so the customer
    sees the Oppgave's Navn (or Navn — Beskrivelse) rather than a
    generic discipline name.

    Skips rows missing a Pris or whose remaining is ≤ 0 (renegotiation
    leftover). Stable order: preserves the order Notion returned them.
    """
    lines: list[InvoiceLine] = []
    for row in eligible:
        canonical = _normalize_discipline(notion_client.task_discipline(row))
        if not canonical:
            continue
        amounts = _row_billable_nok(row, invoice_type)
        if amounts is None:
            continue
        to_bill_nok, new_total_nok, unit_price_nok, discount_fraction = amounts
        amount_ore = int(round(to_bill_nok * 100))
        unit_price_ore = int(round(unit_price_nok * 100))
        if amount_ore <= 0:
            continue
        kategori, income_account = _resolve_kategori_and_account(row)
        product_name = _line_product_name(
            row, kategori
        ) or FIKEN_DISCIPLINE_DISPLAY_NAMES.get(canonical, canonical.title())
        comment = _line_comment(row)
        lines.append(
            InvoiceLine(
                discipline=canonical,
                product_name=product_name,
                comment=comment,
                income_account=income_account,
                unit_price_nok_ore=unit_price_ore,
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
# Product cache (Fiken-side IO)
# ============================================================


async def _ensure_fiken_product(
    company_slug: str,
    discipline: str,
    unit_price_ore: int,
) -> dict[str, Any] | None:
    """Adopt or create the Fiken product for this discipline. Mirrors the
    pre-v2 implementation — the per-line `productId` link still isn't
    sent on free-text lines (so the customer sees the row description,
    not the generic discipline name), but keeping the catalogue mirror
    in step lets Fiken's per-product reports stay correct.

    Returns the product dict (carries `productId`) or None when discipline
    is outside the catalogue.
    """
    product_number = FIKEN_DISCIPLINE_PRODUCT_NUMBERS.get(discipline)
    if not product_number:
        return None
    display_name = FIKEN_DISCIPLINE_DISPLAY_NAMES.get(discipline, discipline.title())
    unit_price = unit_price_ore / 100.0

    async with SessionLocal() as session:
        cached = await session.get(
            FikenProductCache, {"company_slug": company_slug, "discipline": discipline}
        )

    if cached is None:
        existing = await fiken_client.list_products(company_slug)
        adopted = next(
            (p for p in existing if (p.get("productNumber") or "") == product_number),
            None,
        )
        if adopted is None:
            adopted = await fiken_client.create_product(
                company_slug,
                name=display_name,
                product_number=product_number,
                unit_price=unit_price,
            )
        product_id = str(adopted.get("productId") or adopted.get("id") or "")
        if not product_id:
            logger.warning(
                "fiken: created/adopted product %s has no productId in response: %s",
                product_number,
                adopted,
            )
            return adopted
        async with SessionLocal() as session:
            await session.execute(
                pg_insert(FikenProductCache)
                .values(
                    company_slug=company_slug,
                    discipline=discipline,
                    fiken_product_id=product_id,
                    product_number=product_number,
                    last_unit_price_ore=unit_price_ore,
                )
                .on_conflict_do_update(
                    index_elements=["company_slug", "discipline"],
                    set_={
                        "fiken_product_id": product_id,
                        "product_number": product_number,
                        "last_unit_price_ore": unit_price_ore,
                    },
                )
            )
            await session.commit()
        return adopted

    if cached.last_unit_price_ore != unit_price_ore:
        try:
            await fiken_client.update_product(
                company_slug,
                cached.fiken_product_id,
                name=display_name,
                unit_price=unit_price,
            )
        except fiken_client.FikenAPIError as err:
            logger.warning(
                "fiken: failed to PUT product %s price update: %s",
                cached.fiken_product_id,
                err,
            )
        else:
            async with SessionLocal() as session:
                await session.execute(
                    pg_insert(FikenProductCache)
                    .values(
                        company_slug=company_slug,
                        discipline=discipline,
                        fiken_product_id=cached.fiken_product_id,
                        product_number=cached.product_number,
                        last_unit_price_ore=unit_price_ore,
                    )
                    .on_conflict_do_update(
                        index_elements=["company_slug", "discipline"],
                        set_={"last_unit_price_ore": unit_price_ore},
                    )
                )
                await session.commit()
    return {
        "productId": cached.fiken_product_id,
        "productNumber": cached.product_number,
    }


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

    # 3. Build invoice lines (pure math, no I/O).
    lines = _build_line_items(eligible, invoice_type=invoice_type)
    if not lines:
        result.action = "skipped"
        result.note = "no priced rows"
        return result
    result.lines_created = len(lines)

    # 4. Customer resolution. Project → Fakturamottaker → Orgnr.
    #
    # Missing Orgnr (no Fakturamottaker linked, or linked but Orgnr blank)
    # used to hard-fail the task — Notion turned the project's sync dot red
    # after 5 retries and the operator had no way to see why from Notion
    # alone. New behavior: skip the contact lookup entirely, post the draft
    # to Fiken WITHOUT a customerId, and let the operator pick or create
    # one in Fiken's UI before clicking Send. The draft is the review
    # artefact anyway; a missing customer is something Fiken's UI is built
    # to handle.
    orgnr, fakturamottaker_name = await _resolve_project_orgnr(project_page)

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
            new_name = (
                fakturamottaker_name or project_title or f"Kunde {orgnr}"
            ).strip()
            logger.info(
                "send_faktura: orgnr %s not in Fiken customers — "
                "auto-creating Fiken contact %r",
                orgnr,
                new_name,
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

    # 5. Build line payloads. Free-text lines (no productId) so we control
    # the printed text entirely.
    #
    # Field map:
    #  - `description` (bold first line)   ← line.product_name
    #     Kategori label (e.g. "Næring - Interiør"), or a fallback
    #     composed from Navn / Beskrivelse when the row has no Kategori.
    #  - `comment` (smaller sub-line)      ← line.comment
    #     "Navn - Beskrivelse" so the customer sees the category up top
    #     and the specific deliverable underneath.
    #  - `incomeAccount` (kontonummer)     ← line.income_account
    #     Derived from Kategori via FIKEN_KATEGORI_TO_ACCOUNT (3020 for
    #     tjeneste, 3000 for vare, etc.). Defaults to 3020.
    #
    # Two scale notes:
    #  - `unitPrice` on free-text lines is in ØRE (integer cents),
    #    empirically verified — we send the un-discounted price here so
    #    the printed invoice shows "Pris kr X / Rabatt Y% / Sum kr Z"
    #    rather than just a pre-discounted unit price.
    #  - `discount` on lines is PERCENT (0–100, decimals allowed). Fiken
    #    applies it to the line's `unitPrice × quantity` to compute net.
    #    Omitted when 0 — keeps the printed line cleaner.
    line_payloads: list[dict[str, Any]] = []
    for line in lines:
        payload: dict[str, Any] = {
            "description": line.product_name,
            "quantity": 1,
            "unitPrice": line.unit_price_nok_ore,
            "vatType": fiken_client.VAT_TYPE_25_PCT,
            "incomeAccount": line.income_account,
        }
        if line.comment:
            payload["comment"] = line.comment
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

    logger.info(
        "✅ send_faktura: %s draft for %r — %d line(s), customer=%s, draft_id=%s",
        invoice_type,
        project_title or project_page_id,
        len(lines),
        result.customer_match,
        draft_id or "<unknown>",
    )
    return result
