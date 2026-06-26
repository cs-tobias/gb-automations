"""Phase C.2 — write rows in the operator's Notion Faktura DB.

The Faktura DB is the operator-visible record of every sent invoice
(and credit note) the company has produced. Today it's populated by a
Make.com automation; Phase C.3's poller will replace Make by calling
this writer once per newly-sent Fiken record.

Phase C.2 builds the writer + idempotency cache. Nothing calls it
automatically yet — C.3 wires the poller, and there's a manual
`POST /debug/fiken/write-faktura-row` endpoint so operators (and the
CEO) can sanity-check column-by-column formatting against Make's
existing rows before the cutover.

Idempotency: every successful write inserts a `FakturaNotionCache` row
keyed on (company_slug, record_type, fiken_record_id). The cache is
checked BEFORE attempting to write, so re-running this for the same
Fiken record is a fast no-op. Operators are free to delete a Faktura
row in Notion intentionally (e.g. to correct something) — the cache
silently leaves it alone on subsequent runs. Notion is the source of
truth for "does this row exist or not"; Postgres just remembers we
already created it once.

What this module does NOT do:

- Poll Fiken. Phase C.3.
- Update lifecycle statuses on Project / Oppgaver. Phase C.3.
- Re-PATCH existing Faktura rows after creation. Drafts -> sent is a
  one-way write; once a Fiken invoice is sent it's immutable in
  practice (corrections happen via credit notes, which create new
  rows). The cache makes subsequent polls no-op.
- Match Make.com's writes. The two coexist during C.1 → C.4; both can
  write rows, the operator picks the surviving one.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import fiken as fiken_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    FAKTURA_PROPS,
    FAKTURA_TYPE_FAKTURA,
    FAKTURA_TYPE_KREDITNOTA,
    FAKTURAMOTTAKER_PROPS,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FakturaNotionCache

logger = logging.getLogger(__name__)

# `record_type` discriminator passed in to `create_faktura_row`. Mirrors
# the Postgres CHECK constraint. The two values map 1:1 to Fiken's
# `/invoices` vs `/creditNotes` endpoints (and to the Faktura DB's Type
# select options).
RecordType = Literal["faktura", "kreditnota"]


# ---------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------


async def create_faktura_row(
    company_slug: str,
    *,
    fiken_record: dict[str, Any],
    record_type: RecordType,
    project_page_id: str | None = None,
    project_title: str | None = None,
    parent_invoice_number: int | None = None,
) -> str | None:
    """Create a row in the Notion Faktura DB from a Fiken invoice or
    credit-note payload.

    Returns the new Notion page id on a successful write, the cached
    page id on a cache hit (no-op), or None when the engine is disabled
    (no `FAKTURA_DB_ID` set) or the underlying write fails.

    Parameters:
        company_slug: Fiken company slug (e.g. "goldbox-as"). Cache key.
        fiken_record: Raw dict from Fiken's GET /invoices/{id} or
            /creditNotes/{id} response. Shape pinned in Phase B / C.1
            probes; see field-mapping table below.
        record_type: "faktura" or "kreditnota". Drives the Type select
            option and which Fiken id field is read.
        project_page_id: Optional pre-resolved Notion project id. When
            None, the row's Prosjekt relation is left blank (the
            poller's match-by-reference logic supplies this in C.3;
            the debug endpoint can pass None for a no-relation test
            write).
        project_title: Optional pre-resolved project title used as
            `Vår_ref` text fallback. When None, falls back to
            fiken_record.reference.our.

    Idempotent: a successful write inserts into FakturaNotionCache;
    a subsequent call with the same (company_slug, record_type,
    fiken_record_id) is a no-op (returns the cached page id).

    Engine-disabled mode: when `settings.faktura_db_id` is empty,
    logs INFO and returns None. C.2 ships safe-by-default; the
    operator sets the env var in C.4 to activate.
    """
    if not settings.faktura_db_id:
        logger.info(
            "Faktura DB writer: FAKTURA_DB_ID is not set; skipping "
            "(C.2 ships dormant — set the env var in C.4 to enable)."
        )
        return None

    record_id = _extract_record_id(fiken_record, record_type)
    if not record_id:
        logger.warning(
            "Faktura DB writer: Fiken %s has no usable id field — "
            "skipping. payload keys: %s",
            record_type,
            list(fiken_record.keys()),
        )
        return None

    # Idempotency check FIRST — no Notion call needed on a cache hit.
    cached_page_id = await _get_cached_page_id(
        company_slug, record_type, record_id
    )
    if cached_page_id:
        logger.info(
            "Faktura DB writer: %s id=%s already cached as Notion "
            "page %s — no-op.",
            record_type,
            record_id,
            cached_page_id,
        )
        return cached_page_id

    # Resolve the Fakturamottaker relation (best-effort).
    fakturamottaker_page_id = await _resolve_fakturamottaker(
        company_slug, fiken_record
    )

    # Build the property payload + create the page.
    title_text, extra_props = _build_payload(
        fiken_record=fiken_record,
        record_type=record_type,
        project_page_id=project_page_id,
        project_title=project_title,
        fakturamottaker_page_id=fakturamottaker_page_id,
        parent_invoice_number=parent_invoice_number,
    )

    try:
        new_page_id = await notion_client.create_page_in_db(
            settings.faktura_db_id,
            title=title_text,
            title_prop_name=FAKTURA_PROPS["navn"],
            extra_properties=extra_props,
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "Faktura DB writer: create_page_in_db failed for %s id=%s: %s",
            record_type,
            record_id,
            err,
        )
        return None

    # Persist the idempotency marker AFTER the page exists. If the
    # cache insert races (concurrent polls of the same record), the
    # ON CONFLICT DO NOTHING keeps things sane — both pages exist,
    # cache only tracks the first.
    await _mark_cached(
        company_slug=company_slug,
        record_type=record_type,
        fiken_record_id=record_id,
        notion_page_id=new_page_id,
    )
    logger.info(
        "Faktura DB writer: created %s row '%s' (id=%s) for %s id=%s",
        record_type,
        title_text,
        new_page_id,
        record_type,
        record_id,
    )
    return new_page_id


# ---------------------------------------------------------------------
# Internal helpers — pure logic + DB cache
# ---------------------------------------------------------------------


def _extract_record_id(record: dict[str, Any], record_type: RecordType) -> str:
    """Pull the Fiken id field. Invoices carry `invoiceId`; credit notes
    carry `creditNoteId`. We return the digits as a string so the cache
    PK column stays the same shape across both types.
    """
    if record_type == "faktura":
        raw = record.get("invoiceId")
    else:
        raw = record.get("creditNoteId")
    if raw is None:
        return ""
    return str(raw)


def _build_payload(
    *,
    fiken_record: dict[str, Any],
    record_type: RecordType,
    project_page_id: str | None,
    project_title: str | None,
    fakturamottaker_page_id: str | None,
    parent_invoice_number: int | None = None,
) -> tuple[str, dict[str, Any]]:
    """Map a Fiken record dict to (title_text, properties_dict) for
    `create_page_in_db`. Pure function — easy to unit test.

    Title column gets the invoice/credit-note number as a string
    (`Navn`); the same number is also written as a plain integer to
    `Number (fakturanummer)` so Notion can sort numerically.
    """
    # ---- Number fields ----
    number_str = _record_number_str(fiken_record, record_type)
    number_int = _record_number_int(fiken_record, record_type)

    # ---- Customer denormalization ----
    customer = fiken_record.get("customer") or {}
    customer_name = (customer.get("name") or "").strip()

    # ---- References ----
    reference = fiken_record.get("reference") or {}
    # Fiken's invoice payload puts reference fields on a nested
    # `reference` dict, but the older creditNotes shape sometimes
    # surfaces them as flat top-level `yourReference` / `ourReference`.
    # Read both shapes; reference dict wins when present.
    deres_ref_text = (
        reference.get("yours")
        or fiken_record.get("yourReference")
        or ""
    ).strip()
    vaar_ref_text = (
        reference.get("our")
        or fiken_record.get("ourReference")
        or project_title
        or ""
    ).strip()

    # ---- Comment / message ----
    # Fiken's field for the printed "Kommentar" is `invoiceText` on
    # invoices, `creditNoteText` on credit notes (empirically pinned).
    # Read all variants for safety.
    comment_text = (
        fiken_record.get("invoiceText")
        or fiken_record.get("creditNoteText")
        or fiken_record.get("message")
        or fiken_record.get("comment")
        or ""
    ).strip()

    # ---- Issue date ----
    # Fiken returns YYYY-MM-DD strings already. Pass through as-is;
    # Notion accepts ISO-8601 in the `date.start` field.
    issue_date_str = (fiken_record.get("issueDate") or "").strip()

    # ---- URL ----
    url = _build_url(fiken_record, record_type)

    # ---- PDF URL ----
    # Fiken nests the PDF under `invoicePdf` on invoices and
    # `creditNotePdf` on kreditnotas. The
    # `downloadUrlWithFikenNormalUserCredentials` field is the
    # browser-friendly URL — opens straight to the PDF using the
    # operator's existing Fiken session. The `downloadUrl` sibling
    # needs a Bearer token, so we don't use it here.
    pdf_block = (
        fiken_record.get("invoicePdf")
        or fiken_record.get("creditNotePdf")
        or {}
    )
    pdf_url = pdf_block.get(
        "downloadUrlWithFikenNormalUserCredentials"
    ) or pdf_block.get("downloadUrl")

    # ---- Type select ----
    type_label = (
        FAKTURA_TYPE_FAKTURA
        if record_type == "faktura"
        else FAKTURA_TYPE_KREDITNOTA
    )

    # ---- Credit-note parent invoice number ----
    # The operator wants the printable invoice NUMBER in the
    # `Kreditnota til faktura` column (e.g. 10058), NOT the internal
    # `associatedInvoiceId` (a long Fiken-internal id). Resolution
    # order:
    #   1. Explicit `parent_invoice_number` arg — caller resolved it
    #      via our FikenInvoice audit table or by GETting the parent.
    #   2. `associatedInvoiceNumber` on the credit-note payload —
    #      Fiken sometimes returns it directly (newer accounts).
    #   3. Skip entirely. Writing the `associatedInvoiceId` here would
    #      be misleading (it's the wrong number — operators looking at
    #      Faktura DB would see e.g. 7653473504 and not recognize it as
    #      a faktura number). Better to leave blank than mislead.
    kreditnota_til_int: int | None = None
    if record_type == "kreditnota":
        if parent_invoice_number is not None:
            kreditnota_til_int = int(parent_invoice_number)
        else:
            raw_parent_no = fiken_record.get("associatedInvoiceNumber")
            if raw_parent_no is not None:
                try:
                    kreditnota_til_int = int(str(raw_parent_no))
                except (TypeError, ValueError):
                    kreditnota_til_int = None

    # ---- Net total ----
    # `net` is in øre (Phase B pin); Notion expects display NOK.
    # Convert: 624000 øre → 6240.00 NOK.
    netto_nok: float | None = None
    raw_net = fiken_record.get("net")
    if raw_net is not None:
        try:
            netto_nok = float(raw_net) / 100.0
        except (TypeError, ValueError):
            netto_nok = None

    # ---- Build the properties dict ----
    props: dict[str, Any] = {}

    if number_int is not None:
        props[FAKTURA_PROPS["fakturanummer"]] = {"number": number_int}
    if kreditnota_til_int is not None:
        props[FAKTURA_PROPS["kreditnota_til"]] = {
            "number": kreditnota_til_int
        }
    if netto_nok is not None:
        props[FAKTURA_PROPS["netto"]] = {"number": netto_nok}

    props[FAKTURA_PROPS["type"]] = {"select": {"name": type_label}}

    if comment_text:
        props[FAKTURA_PROPS["kommentar"]] = _rich_text(comment_text)
    if deres_ref_text:
        props[FAKTURA_PROPS["deres_ref"]] = _rich_text(deres_ref_text)
    if vaar_ref_text:
        props[FAKTURA_PROPS["vaar_ref"]] = _rich_text(vaar_ref_text)
    if customer_name:
        props[FAKTURA_PROPS["fakturamottaker_tekst"]] = _rich_text(
            customer_name
        )
    if issue_date_str:
        props[FAKTURA_PROPS["dato"]] = {"date": {"start": issue_date_str}}
    if url:
        props[FAKTURA_PROPS["url"]] = {"url": url}
    if pdf_url:
        # `Faktura PDF` is a files & media column in the operator's DB
        # (their existing Make-written rows embed the PDF, so clicking it
        # downloads the file). Write it as an EXTERNAL file entry rather
        # than a plain url so new rows match — Notion renders an external
        # file as a clickable attachment. `name` must end in a file-ish
        # label; we use the invoice number so the download is identifiable.
        pdf_name = f"{number_str}.pdf" if number_str else "faktura.pdf"
        props[FAKTURA_PROPS["pdf_url"]] = {
            "files": [
                {
                    "name": pdf_name,
                    "type": "external",
                    "external": {"url": pdf_url},
                }
            ]
        }

    # Relations — blank when unresolved.
    if project_page_id:
        props[FAKTURA_PROPS["prosjekt"]] = {
            "relation": [{"id": project_page_id}]
        }
    if fakturamottaker_page_id:
        props[FAKTURA_PROPS["fakturamottaker"]] = {
            "relation": [{"id": fakturamottaker_page_id}]
        }

    return number_str, props


def _record_number_str(record: dict[str, Any], record_type: RecordType) -> str:
    """The invoice/credit-note number as a printable string for the
    title column. Falls back to "Faktura {id}" when no number is set
    (shouldn't happen for sent records, but stay defensive)."""
    if record_type == "faktura":
        raw = record.get("invoiceNumber")
    else:
        raw = record.get("creditNoteNumber")
    if raw is not None:
        return str(raw)
    record_id = _extract_record_id(record, record_type)
    label = "Faktura" if record_type == "faktura" else "Kreditnota"
    return f"{label} {record_id}" if record_id else label


def _record_number_int(
    record: dict[str, Any], record_type: RecordType
) -> int | None:
    """The number as a plain int for `Number (fakturanummer)`. None
    when the source value can't parse as int (e.g. the title fallback
    above kicked in)."""
    if record_type == "faktura":
        raw = record.get("invoiceNumber")
    else:
        raw = record.get("creditNoteNumber")
    if raw is None:
        return None
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return None


def _build_url(record: dict[str, Any], record_type: RecordType) -> str:
    """Compose the Fiken UI URL for a sent invoice or credit note.

    Empirically pinned (2026-06-21): Fiken's web UI uses
        https://fiken.no/foretak/{slug}/handel/salg/{saleId}
    for BOTH invoices and credit notes. `saleId` is on the
    `sale.saleId` block of the GET /invoices/{id} or
    /creditNotes/{id} response. The earlier `/faktura/{invoiceId}`
    and `/kreditnota/{creditNoteId}` paths return blank pages —
    they look right but don't resolve to anything.

    Falls back to an empty string when company_slug or saleId is
    missing (the column on the Notion Faktura row just stays blank;
    operator can find the record via Faktura nr in Fiken).
    """
    company_slug = settings.fiken_company_slug or ""
    sale = record.get("sale") or {}
    sale_id = sale.get("saleId")
    if not company_slug or sale_id is None:
        return ""
    return f"https://fiken.no/foretak/{company_slug}/handel/salg/{sale_id}"


def _rich_text(text: str) -> dict[str, Any]:
    """Build a Notion `rich_text` property value from a plain string."""
    return {"rich_text": [{"text": {"content": text}}]}


# ---------------------------------------------------------------------
# Idempotency cache (Postgres)
# ---------------------------------------------------------------------


async def _get_cached_page_id(
    company_slug: str, record_type: RecordType, record_id: str
) -> str | None:
    """Return the Notion page id we already wrote for this Fiken
    record, or None if not yet written. Pure SELECT — no side effects.
    """
    async with SessionLocal() as session:
        stmt = select(FakturaNotionCache.notion_page_id).where(
            FakturaNotionCache.company_slug == company_slug,
            FakturaNotionCache.record_type == record_type,
            FakturaNotionCache.fiken_record_id == record_id,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        return row or None


async def _mark_cached(
    *,
    company_slug: str,
    record_type: RecordType,
    fiken_record_id: str,
    notion_page_id: str,
) -> None:
    """Insert the idempotency marker. ON CONFLICT DO NOTHING handles
    the race where two polls reconcile the same record concurrently
    (both pages exist, cache tracks the first one in)."""
    async with SessionLocal() as session:
        stmt = (
            pg_insert(FakturaNotionCache)
            .values(
                company_slug=company_slug,
                record_type=record_type,
                fiken_record_id=fiken_record_id,
                notion_page_id=notion_page_id,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "company_slug",
                    "record_type",
                    "fiken_record_id",
                ]
            )
        )
        await session.execute(stmt)
        await session.commit()


# ---------------------------------------------------------------------
# Customer → Notion Fakturamottaker resolver (best-effort)
# ---------------------------------------------------------------------


_ORGNR_DIGITS_RE = re.compile(r"\D+")


def _normalize_orgnr(raw: str | None) -> str | None:
    """Digits-only Orgnr, or None if nothing digit-y remains. Mirrors
    the duplicate helper in sync_fiken_invoice.py — kept local so this
    module doesn't have to import from the engine."""
    if not raw:
        return None
    digits = _ORGNR_DIGITS_RE.sub("", str(raw))
    return digits or None


async def _resolve_fakturamottaker(
    company_slug: str, fiken_record: dict[str, Any]
) -> str | None:
    """Walk customer.contactId → Fiken /contacts/{id} → orgnr →
    Notion Fakturamottaker row id. Returns None on any miss.

    Three short-circuits:

    1. No FAKTURAMOTTAKER_DB_ID configured → skip (legacy installs).
    2. No customer.contactId on the record → skip (Fiken occasionally
       omits this; the relation just stays blank).
    3. Fiken contact has no organizationNumber → skip (a Brreg-by-name
       fallback would mirror what send_faktura does, but C.2 keeps it
       simple — the customer name is denormalized into
       `Fakturamottaker tekst` regardless).
    """
    if not getattr(settings, "fakturamottaker_db_id", ""):
        return None

    customer = fiken_record.get("customer") or {}
    fiken_contact_id = customer.get("contactId")
    if not fiken_contact_id:
        return None

    # Some Fiken responses already include organizationNumber inline on
    # the customer block; use it without an extra round-trip when present.
    orgnr_raw = customer.get("organizationNumber")
    if not orgnr_raw:
        contact = await fiken_client.get_contact(
            company_slug, str(fiken_contact_id)
        )
        if not contact:
            return None
        orgnr_raw = contact.get("organizationNumber")

    orgnr = _normalize_orgnr(orgnr_raw)
    if not orgnr:
        return None

    try:
        rows = await notion_client.query_database(
            settings.fakturamottaker_db_id
        )
    except Exception as err:  # noqa: BLE001
        logger.info(
            "Faktura DB writer: fakturamottaker DB query failed (%s) — "
            "leaving relation blank.",
            err,
        )
        return None

    orgnr_prop = FAKTURAMOTTAKER_PROPS["orgnr"]
    for row in rows:
        raw = notion_client.read_rich_text_prop(row, orgnr_prop)
        if _normalize_orgnr(raw) == orgnr:
            return row.get("id") or None
    return None
