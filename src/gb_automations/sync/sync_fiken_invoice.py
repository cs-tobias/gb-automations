"""Notion-button → Fiken-draft creation engine (Phase B).

Worker entrypoint for a `fiken_invoice_create` task. Drains a click of the
per-project "Opprett oppstartsfaktura" / "Opprett sluttfaktura" Notion
button. For an `oppstart` task:

    1. Read the live Project page (title only — oppstart split is
       hardcoded at 50%).
    2. List the project's Oppgaver, keep deliverable rows (`Type` ∈
       DISCIPLINE_KEYS) where `Skal oppstartsfaktureres` is ticked and
       `Oppstartsfakturert andel` < 1.
    3. Resolve the Fiken customer (project title → Fiken contact `name`
       case-insensitive). On no match, fail loudly so the operator either
       renames the Fiken contact or hand-creates the draft.
    4. Group eligible rows by (discipline, Pris) — same discipline at
       different prices become two lines so the audit is honest. Quantity
       per line = count × fraction.
    5. Ensure a Fiken product exists for each discipline (FikenProductCache
       holds the mapping). If the cached unit price differs from the
       Notion-side price we PUT the update; if no product exists we
       create one.
    6. POST a DRAFT invoice. Drafts are NOT sent — the user reviews and
       clicks Send in Fiken's UI.
    7. Persist the audit trail to Postgres (FikenInvoice + one
       FikenInvoiceLine per Oppgave billed). Stamp each Oppgave in Notion
       with the new `Oppstartsfakturert andel` and clear the Skal-checkbox.
       Write the draft URL onto the Project.

For a `slutt` task the eligibility filter is "Skal sluttfaktureres ticked
AND remainder > 0", where remainder = `1 - Oppstartsfakturert andel`. The
quantity per line is `sum(remainders) × 1.0` (no further split — slutt
bills the rest). The same audit + stamping path applies, using the slutt
columns.

Idempotency:
  - Re-clicking the same button with no eligible rows logs "nothing to
    bill" and returns without touching Fiken (no zero-line draft, no
    Notion stamps).
  - The button webhook's dedup index collapses double-clicks during the
    in-flight window to one task.

Out of scope (v1):
  - Cancelling / deleting drafts (operator does it in Fiken UI).
  - Auto-send (drafts stay drafts until user clicks Send in Fiken).
  - Reading the draft back into Notion before send (the Fiken→Notion
    poller in Phase C catches it once sent).
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
    COMPANIES_PROPS,
    DISCIPLINE_KEYS,
    FAKTURAMOTTAKER_PROPS,
    FAKTURERT_LABEL_KREDITERT,
    FAKTURERT_LABEL_OPPSTART,
    FAKTURERT_LABEL_SLUTT,
    FAKTURERT_LABEL_SLUTT_FULL,
    FAKTURERT_LABELS_SLUTT_DONE,
    OPPGAVER_PROPS,
    PROJECTS_FIKEN_PROPS,
    PROJECTS_KUNDER_PROP,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import (
    FikenInvoice,
    FikenInvoiceLine,
    FikenProductCache,
)

logger = logging.getLogger(__name__)


# Per-discipline Fiken product number stems. The engine uses these as the
# stable Fiken-side identifier (productNumber) — distinct from the
# numeric productId Fiken auto-assigns and which the cache stores.
# Outside the discipline set → no product mapping (the row is then
# emitted as a free-text line; this should not happen given the
# eligibility filter, but it's defensive).
FIKEN_DISCIPLINE_PRODUCT_NUMBERS = {
    "interior": "goldbox-interior",
    "exterior": "goldbox-exterior",
    "animation": "goldbox-animation",
    "other": "goldbox-other",
}

# Display names used on the Fiken invoice line. Norwegian to match what
# the customer sees on the printed invoice; the discipline keys are
# canonical (English) for code consistency.
FIKEN_DISCIPLINE_DISPLAY_NAMES = {
    "interior": "Interiør",
    "exterior": "Eksteriør",
    "animation": "Animasjon",
    "other": "Annet",
}

# Hardcoded oppstart split. The brief locked in 50/50; making it
# configurable added an Oppstartsandel % field on every project that
# nobody actually wanted to think about per-click. One button = always
# 50%. Slutt always bills the remainder (1 - whatever oppstart booked).
OPPSTART_FRACTION = 0.5

# Default payment terms (days from issue date) on a Fiken draft. Fiken
# requires this field on every draft. The operator can edit it in Fiken's
# UI before sending if a particular customer has different terms.
DEFAULT_DAYS_UNTIL_DUE_DATE = 14


@dataclass
class InvoiceLine:
    """One Fiken invoice line. One per eligible Oppgave — no grouping.

    The Oppgave's `Navn` is the description so the customer sees what
    each line is for. The discipline is kept for the audit trail and
    to pick the right Fiken product (incomeAccount routing).
    """

    discipline: str            # canonical key, e.g. "interior"
    description: str           # the Oppgave's Navn — what shows on the invoice
    quantity: float            # billable fraction for this row (e.g. 0.5)
    unit_price_ore: int        # NOK øre (integer cents) — captured at run time
    oppgave_page_id: str       # the single row this line came from
    fraction: float            # alias of quantity, kept for audit clarity


@dataclass
class InvoiceCreateResult:
    project_page_id: str
    invoice_type: str
    action: str = "ok"  # ok | skipped | failed
    note: str | None = None
    # Counters surfaced in worker logs.
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
    ("interior"). Returns None when the label isn't a recognized
    discipline (Klargjøre modell, Korreksjonsrunde, blank — all of which
    the eligibility filter should already have dropped, but defensive).
    """
    if not raw:
        return None
    return DISCIPLINE_KEYS.get(raw.strip().lower())


def _row_fraction_for_invoice_type(
    row: dict[str, Any], invoice_type: str
) -> float:
    """Return how much of this row CAN still be billed on the next
    invoice of `invoice_type`. Reads the row's `Fakturert` multi-select.

    Oppstart eligibility (fraction > 0):
      - row has NO Fakturert label yet (never billed).

    Slutt eligibility (fraction > 0):
      - row has no slutt-side label (Sluttfakturert or
        Sluttfakturert (full));
      - and no Kreditert.

    A row with Kreditert is always 0 — credit-noted rows must not bill
    again.

    Fraction returned is the *cap* before the engine's OPPSTART_FRACTION
    (0.5) gets applied — for slutt it's the remainder (0.5 if oppstart
    ran, 1.0 if it didn't).
    """
    labels = notion_client.read_multi_select_names(
        row, OPPGAVER_PROPS["billed_status"]
    )
    if FAKTURERT_LABEL_KREDITERT in labels:
        return 0.0

    if invoice_type == "oppstart":
        # Already billed in any way → no oppstart room.
        if (
            FAKTURERT_LABEL_OPPSTART in labels
            or FAKTURERT_LABEL_SLUTT in labels
            or FAKTURERT_LABEL_SLUTT_FULL in labels
        ):
            return 0.0
        return 1.0

    if invoice_type == "slutt":
        # Already slutt-billed (either via "Sluttfakturert" or
        # "Sluttfakturert (full)") → no room. Otherwise: remainder is
        # 0.5 if oppstart ran, 1.0 if it didn't.
        if any(lbl in labels for lbl in FAKTURERT_LABELS_SLUTT_DONE):
            return 0.0
        if FAKTURERT_LABEL_OPPSTART in labels:
            return 1.0 - OPPSTART_FRACTION
        return 1.0

    raise ValueError(f"invoice_type must be oppstart|slutt, got {invoice_type!r}")


def _eligible_rows(
    rows: list[dict[str, Any]], invoice_type: str
) -> list[tuple[dict[str, Any], float]]:
    """Filter Notion rows to those eligible for this invoice run.

    Two-pass picker semantics:

      1. PARTIAL mode — if ANY discipline row has the matching
         `Skal …faktureres` checkbox ticked, use only the ticked
         rows. Lets the operator stagger billing ("invoice these 3
         now, hold the rest for next month").
      2. BILL-ALL mode — if NO row has the checkbox ticked, fall back
         to "bill everything that still has billing room." Lets the
         operator click "Opprett sluttfaktura" on a wrapped-up project
         without ticking 20 boxes first.

    A row passes regardless of mode only if:
      - Type is a recognized discipline (Klargjøre modell /
        Korreksjonsrunde / blank are skipped).
      - Has billing room left (`_row_fraction_for_invoice_type > 0`).

    Returns list of (row, max_fraction_available). The caller decides
    how much of `max_fraction_available` to actually bill.
    """
    if invoice_type == "oppstart":
        checkbox_key = "should_invoice_at_start"
    elif invoice_type == "slutt":
        checkbox_key = "should_invoice_at_end"
    else:
        raise ValueError(f"invoice_type must be oppstart|slutt, got {invoice_type!r}")
    checkbox_prop = OPPGAVER_PROPS[checkbox_key]

    # First pass: collect every discipline row with billing room, plus
    # whether it's ticked. Two list comprehensions over the same pass
    # would be redundant; we hold both and decide mode below.
    candidates: list[tuple[dict[str, Any], float, bool]] = []
    for row in rows:
        discipline = notion_client.task_discipline(row)
        canonical = _normalize_discipline(discipline)
        if not canonical:
            continue
        max_fraction = _row_fraction_for_invoice_type(row, invoice_type)
        if max_fraction <= 0.0:
            continue
        ticked = notion_client.read_checkbox_prop(row, checkbox_prop)
        candidates.append((row, max_fraction, ticked))

    any_ticked = any(ticked for _, _, ticked in candidates)
    if any_ticked:
        return [(row, frac) for row, frac, ticked in candidates if ticked]
    # Bill-all fallback: no explicit picks → bill every row with room.
    return [(row, frac) for row, frac, _ in candidates]


def _build_line_items(
    eligible: list[tuple[dict[str, Any], float]],
    *,
    invoice_type: str,
) -> list[InvoiceLine]:
    """Compose Fiken invoice lines from the eligible rows — ONE line
    per Oppgave (no grouping). Each line's description is the Oppgave's
    Navn so the customer sees what each charge is for on the invoice.

    Rules:
      - Quantity per line:
          oppstart → min(max_fraction, OPPSTART_FRACTION) — 0.5 for a
                     fresh row, capped by what's left to bill.
          slutt    → max_fraction — the full remainder (0.5 if oppstart
                     already happened, 1.0 if not).
      - Unit price snapped to NOK øre (integer cents). Round half away
        from zero to avoid systematic under-billing on .5-øre tails.

    Rows without a `Pris` set produce a WARN and are skipped — the audit
    must be honest about why a row was dropped, not silently default to 0.

    Stable order: by Oppgave page id (preserves the order Notion
    returned them in, which preserves the team's hand-ordering).
    """
    fraction_cap = (
        OPPSTART_FRACTION if invoice_type == "oppstart" else 1.0
    )

    lines: list[InvoiceLine] = []
    for row, max_fraction in eligible:
        discipline_raw = notion_client.task_discipline(row)
        canonical = _normalize_discipline(discipline_raw)
        if not canonical:
            continue  # eligibility filter should have caught this
        price_nok = notion_client.read_number_prop(
            row, OPPGAVER_PROPS["price_per_row"]
        )
        if price_nok is None or price_nok <= 0:
            logger.warning(
                "fiken_invoice_create: row %s has no Pris set — skipping",
                row.get("id"),
            )
            continue
        unit_price_ore = int(round(price_nok * 100))
        billable_fraction = min(max_fraction, fraction_cap)
        if billable_fraction <= 0.0:
            continue
        description = (
            (notion_client.extract_page_title(row) or "").strip()
            or FIKEN_DISCIPLINE_DISPLAY_NAMES.get(canonical, canonical.title())
        )
        lines.append(
            InvoiceLine(
                discipline=canonical,
                description=description,
                quantity=billable_fraction,
                unit_price_ore=unit_price_ore,
                oppgave_page_id=row.get("id", ""),
                fraction=billable_fraction,
            )
        )
    return lines


def _normalize_orgnr(raw: str | None) -> str:
    """Strip every non-digit so '123 456 789' and '123-456-789' compare
    equal to '123456789'. Returns "" if the input is empty/None or
    contains no digits.
    """
    if not raw:
        return ""
    return "".join(ch for ch in raw if ch.isdigit())


# ============================================================
# Notion-side: walk Project → Kunder → Fakturamottaker → Orgnr
# ============================================================


async def _resolve_project_orgnr(
    project_page: dict[str, Any],
) -> tuple[str | None, str | None]:
    """Walk Project → Kunder → Fakturamottaker → Orgnr.

    Returns `(orgnr_digits_only, kunder_title)`. `kunder_title` is the
    Notion Kunder row's title (the customer's display name) — used by
    the engine to auto-create a Fiken contact when no matching customer
    exists yet. Either tuple slot can be None if the corresponding
    Notion read failed; the engine handles each independently.
    """
    kunder_ids = notion_client.read_relation_ids(project_page, PROJECTS_KUNDER_PROP)
    if not kunder_ids:
        return None, None

    # A project SHOULD link to one Kunder; if more, walk the first that
    # yields an org number. Forgiving read; Notion's UI doesn't enforce
    # cardinality either.
    first_kunder_name: str | None = None
    for kunder_id in kunder_ids:
        try:
            kunder_page = await notion_client.get_page(kunder_id)
        except Exception as err:  # noqa: BLE001
            logger.info(
                "fiken: skipping Kunder %s (read failed: %s)", kunder_id, err
            )
            continue
        kunder_name = (
            notion_client.extract_page_title(kunder_page) or ""
        ).strip() or None
        if first_kunder_name is None:
            first_kunder_name = kunder_name

        fakt_ids = notion_client.read_relation_ids(
            kunder_page, COMPANIES_PROPS["fakturamottaker"]
        )
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
            raw = notion_client.read_rich_text_prop(
                fakt_page, FAKTURAMOTTAKER_PROPS["orgnr"]
            )
            normalized = _normalize_orgnr(raw)
            if normalized:
                return normalized, kunder_name or first_kunder_name
    return None, first_kunder_name


# ============================================================
# Product cache + customer resolution (Fiken-side IO)
# ============================================================


async def _ensure_fiken_product(
    company_slug: str,
    discipline: str,
    unit_price_ore: int,
) -> dict[str, Any] | None:
    """Ensure a Fiken product exists for this discipline at the given
    unit price. Returns the product dict (carries `productId`) or None
    when discipline is outside the catalog.

    Adoption-by-productNumber: if a product with our productNumber
    exists in Fiken (set up by hand or from a prior run before the
    cache was populated), we adopt it instead of duplicating.

    Price drift: when the cached price differs from the new one, PUT
    the update so Fiken's catalogue reflects current pricing. The
    invoice line ALSO carries unitAmount, so even if the PUT fails
    the draft still has the right price.
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
        # First sighting — adopt-or-create.
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

    # Cached — check for price drift.
    if cached.last_unit_price_ore != unit_price_ore:
        try:
            await fiken_client.update_product(
                company_slug,
                cached.fiken_product_id,
                name=display_name,
                unit_price=unit_price,
            )
        except fiken_client.FikenAPIError as err:
            # Don't block the invoice — the line carries unitAmount so
            # the draft is still correct. Log loudly so the operator
            # fixes the catalogue drift out of band.
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
# Top-level engine entrypoint
# ============================================================


async def create_fiken_invoice(
    project_page_id: str, invoice_type: str
) -> InvoiceCreateResult:
    """Worker entrypoint for a `fiken_invoice_create` task.

    See the module docstring for the full algorithm. Returns a dataclass
    the worker handler reads to decide `done` vs. `failed`.

    Any exception that surfaces means "retry with backoff"; an `action`
    of "failed" without an exception means "terminal misconfig — don't
    retry into the same brick wall" (e.g. Fiken rejected the draft —
    the operator must fix something before the retry helps).

    Observability: each run logs a one-line summary to docker logs
    (look for the ✅/↻ prefix). The Project's `Siste fiken-utkast` URL
    property is also written on success.
    """
    result = InvoiceCreateResult(
        project_page_id=project_page_id, invoice_type=invoice_type
    )
    if invoice_type not in ("oppstart", "slutt"):
        result.action = "failed"
        result.note = f"unknown invoice_type {invoice_type!r}"
        return result
    if not settings.fiken_company_slug:
        result.action = "failed"
        result.note = "FIKEN_COMPANY_SLUG is not configured"
        return result
    company_slug = settings.fiken_company_slug

    # 1. Read live project page.
    try:
        project_page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken_invoice_create: failed to GET project %s", project_page_id
        )
        result.action = "failed"
        result.note = f"Notion get_page (project): {err}"
        return result

    project_title = (notion_client.extract_page_title(project_page) or "").strip()

    # 2. Read all Oppgaver under this project.
    try:
        rows = await notion_client.oppgaver_for_project(project_page_id)
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken_invoice_create: failed to list oppgaver for %s",
            project_page_id,
        )
        result.action = "failed"
        result.note = f"Notion oppgaver_for_project: {err}"
        return result

    eligible = _eligible_rows(rows, invoice_type)
    result.eligible_rows = len(eligible)
    if not eligible:
        logger.info(
            "fiken_invoice_create: no eligible Oppgaver for %s (%s) — nothing to bill",
            invoice_type,
            project_title or project_page_id,
        )
        result.action = "skipped"
        result.note = "no eligible rows"
        return result

    # 3. Build invoice lines (pure math, no I/O).
    lines = _build_line_items(eligible, invoice_type=invoice_type)
    if not lines:
        # Every eligible row was missing a Pris (logged WARN per row).
        result.action = "skipped"
        result.note = "no priced rows"
        return result
    result.lines_created = len(lines)

    # 4. Customer resolution. Fiken's API requires a numeric customerId
    # on every draft (the "type the org number, Fiken auto-fills" thing
    # is UI-only). So:
    #   1. Read Orgnr + Kunder name from Notion (Project → Kunder →
    #      Fakturamottaker → Orgnr).
    #   2. GET /contacts and find the one with matching organizationNumber.
    #   3. If no match: auto-create a Fiken contact using the Kunder
    #      row's name + the Orgnr. Operator can fill address/email in
    #      Fiken later. This makes the engine self-healing — first
    #      invoice for a new customer Just Works.
    #   4. If no Orgnr in Notion at all: hard fail. We can't post a
    #      draft without a customer, and we have no way to identify one.
    orgnr, kunder_name = await _resolve_project_orgnr(project_page)
    customer_id: int | None = None
    customer_label = orgnr or "<no orgnr>"
    if not orgnr:
        logger.warning(
            "fiken_invoice_create: project %r has no Orgnr in its "
            "Kunder → Fakturamottaker chain — link a Fakturamottaker "
            "with an Orgnr before retrying",
            project_title,
        )
        result.action = "failed"
        result.note = "no Orgnr in Notion (Project → Kunder → Fakturamottaker)"
        return result

    try:
        contacts = await fiken_client.list_contacts(
            company_slug, customer=True
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken_invoice_create: list_contacts failed for slug=%s",
            company_slug,
        )
        result.action = "failed"
        result.note = f"Fiken list_contacts: {err}"
        return result

    target = orgnr  # already digits-only from _resolve_project_orgnr
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

    if customer_id is None:
        # No matching customer in Fiken — auto-create one from the
        # Notion Kunder row.
        new_name = (kunder_name or project_title or f"Kunde {orgnr}").strip()
        logger.info(
            "fiken_invoice_create: orgnr %s not in Fiken customers — "
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
                "fiken_invoice_create: failed to auto-create Fiken contact "
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
                "fiken_invoice_create: auto-created contact carries no "
                "contactId/id (%s) — Fiken's response shape changed?",
                created,
            )
            result.action = "failed"
            result.note = "auto-created Fiken contact has no contactId"
            return result

    result.customer_match = customer_label

    # 5. Build line payloads — free-text lines, no productId.
    #
    # Why no productId: when a line carries productId, Fiken's UI
    # displays the PRODUCT name (e.g. "Interiør") instead of the line
    # description (the Oppgave's Navn we want to show). Free-text lines
    # render the description as-is. We carry the discipline through the
    # incomeAccount field on the line directly, so the booking still
    # routes to the right ledger account.
    #
    # Field names sent on each line:
    #   - description   per-Oppgave Navn from Notion
    #   - quantity      decimal (Fiken accepts floats)
    #   - unitPrice     INTEGER ØRE for free-text lines. Yes — the field
    #                   is called "unitPrice" in BOTH directions but its
    #                   scale differs by line type: lines linked to a
    #                   productId use unitPrice=kroner (because the
    #                   product carries its own scale), free-text lines
    #                   use unitPrice=øre. Empirically observed: sending
    #                   13500 on a free-text line displayed as 135,00 NOK
    #                   in Fiken's UI (i.e. Fiken treated 13500 as øre).
    #                   So we send the unit_price_ore we've been carrying
    #                   internally, no conversion.
    #   - vatType       "HIGH" for 25% MVA
    #   - incomeAccount "3020" (services) — routes the booking
    line_payloads: list[dict[str, Any]] = []
    for line in lines:
        payload: dict[str, Any] = {
            "description": line.description,
            "quantity": round(line.quantity, 4),
            "unitPrice": line.unit_price_ore,
            "vatType": fiken_client.VAT_TYPE_25_PCT,
            "incomeAccount": fiken_client.INCOME_ACCOUNT_SERVICES_VAT,
        }
        line_payloads.append(payload)

    # 6. POST the draft.
    issue_date = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        draft = await fiken_client.create_invoice_draft(
            company_slug,
            customer_id=customer_id,
            issue_date=issue_date,
            days_until_due_date=DEFAULT_DAYS_UNTIL_DUE_DATE,
            reference=project_title or project_page_id,
            lines=line_payloads,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "fiken_invoice_create: draft POST failed for %s", project_page_id
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
            "fiken_invoice_create: Fiken accepted the draft but the response "
            "carries no draftId/id: %s",
            draft,
        )
    result.fiken_invoice_id = draft_id or None

    # Build the URL Fiken's web UI uses. The POST response only carries
    # the numeric draftId, but the UI's draft page is keyed on `uuid`
    # (`/foretak/{slug}/fakturautkast/{uuid}`). Fetch the draft once to
    # read its uuid. Best-effort: if the GET fails, fall back to the
    # numeric-id URL (which 404s in the UI but at least preserves the id
    # in the Notion field for debugging).
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
                "fiken_invoice_create: GET draft %s for uuid failed: %s",
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
                        fraction=line.fraction,
                        unit_price_ore=line.unit_price_ore,
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

    # 8. Stamp each billed Oppgave in Notion + write draft URL on Project.
    # Note: this is the LAST step — if it fails partway, the audit trail
    # above tells the next run what was already done so we don't double-bill.
    #
    # For oppstart runs: add label `Oppstartsfakturert`.
    # For slutt runs: add `Sluttfakturert` if the row already had
    #   `Oppstartsfakturert` (50/50 split), else `Sluttfakturert (full)`
    #   (single invoice billing the whole row).
    # We re-read the row immediately before writing so concurrent edits
    # in Notion between this engine's start and now don't get clobbered
    # (e.g. an operator added `Kreditert` mid-run).
    for line in lines:
        opg_id = line.oppgave_page_id
        if not opg_id:
            continue
        try:
            opg = await notion_client.get_page(opg_id)
            existing = notion_client.read_multi_select_names(
                opg, OPPGAVER_PROPS["billed_status"]
            )
            if invoice_type == "oppstart":
                label_to_add = FAKTURERT_LABEL_OPPSTART
            else:
                # Slutt: depend on whether oppstart already happened.
                if FAKTURERT_LABEL_OPPSTART in existing:
                    label_to_add = FAKTURERT_LABEL_SLUTT
                else:
                    label_to_add = FAKTURERT_LABEL_SLUTT_FULL
            await notion_client.set_oppgave_billing_state(
                opg_id,
                invoice_type=invoice_type,
                label_to_add=label_to_add,
                existing_labels=existing,
            )
        except Exception as err:  # noqa: BLE001
            # The draft is already in Fiken and the audit trail is
            # persisted; failing here just means a row's label isn't
            # written yet. Operator can fix by hand. Don't fail the
            # whole task.
            logger.warning(
                "fiken_invoice_create: failed to stamp Oppgave %s: %s",
                opg_id,
                err,
            )

    if draft_url:
        try:
            await notion_client.set_project_last_draft_url(
                project_page_id, draft_url
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "fiken_invoice_create: failed to write last_draft_url on %s: %s",
                project_page_id,
                err,
            )

    logger.info(
        "✅ fiken_invoice_create: %s draft for %r — %d line(s), "
        "customer=%s, draft_id=%s",
        invoice_type,
        project_title or project_page_id,
        len(lines),
        result.customer_match,
        draft_id or "<unknown>",
    )
    return result
