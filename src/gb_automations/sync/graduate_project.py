"""Phase C.3a — manually graduate one project's sent Fiken invoices into Notion.

This is the trigger-driven variant of the future hourly poller (C.3b).
Same engine, narrower scope: instead of scanning all Fiken invoices
across all companies, this one targets a single Notion project and:

  1. Lists every SENT invoice on the Fiken side for the project's
     company (via /invoices — drafts live at a separate endpoint
     and aren't returned).
  2. Matches each sent invoice to this specific project via TWO
     strategies:
        a) PRIMARY: draft_uuid → invoiceDraftUuid. Our send_faktura
           stores the draft's `uuid` on the FikenInvoice row at
           draft-creation time; Fiken preserves it as
           `invoiceDraftUuid` on the sent invoice (the invoice's
           `invoiceId` is freshly minted at send-time so we can't
           match on id). Exact FK match, zero ambiguity.
        b) FALLBACK 1: reference.our exact match against project title
           (casefold + strip). Catches invoices the CEO created
           manually in Fiken's UI with the project name as Vår
           referanse.
        c) FALLBACK 2: invoiceText (Kommentar) exact match against
           project title (casefold + strip). Catches the common case
           where the CEO doesn't fill in Vår referanse but writes a
           meaningful comment — name the Notion project the same
           string as the Kommentar and Sjekk fiken pulls the invoice.
  3. For each match:
        - Calls notion_faktura_db.create_faktura_row to land it in
          the Faktura DB (idempotent via FakturaNotionCache).
        - When the match was PRIMARY (we know which Oppgaver were
          on the invoice via FikenInvoiceLine), graduates statuses:
          set Fakturert status to "Oppstart fakturert" (oppstart) or
          "Fakturert" (slutt) on each Oppgave; set Project Faktura
          status to the right history value; clear both handling
          columns to blank.
        - For FALLBACK matches (no per-Oppgave audit trail), the
          Faktura DB row still lands but Oppgave + Project status
          are NOT touched — we don't know which rows were on the
          invoice, and a guess would falsely flip statuses.
  4. Records `sent_at`, `sent_url`, `invoice_number` on the
     FikenInvoice row so subsequent runs are no-ops (and a future
     `/debug/fiken/sent-invoices` debug endpoint can list what's
     been graduated vs what's still pending).
  5. Returns a structured summary so the operator can see exactly
     what changed.

The graduation step is INTENTIONALLY conservative on the no-Audit-
trail path: better to leave status columns alone and let the operator
manually flip them than to fabricate a graduation that touches the
wrong rows.

C.3a is operator-triggered (via `POST /debug/fiken/graduate-project`).
C.3b will reuse this engine in a per-project loop driven by an
APScheduler cron — almost zero new logic at that point.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import fiken as fiken_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    FAKTURERT_STATUS_50,
    FAKTURERT_STATUS_FULL,
    FAKTURERT_STATUS_KREDITERT,
    FAKTURA_STATUS_50,
    FAKTURA_STATUS_FULL,
    FAKTURA_STATUS_KREDITERT,
    OPPGAVER_PROPS,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FikenInvoice, FikenInvoiceLine
from gb_automations.sync import notion_faktura_db

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Result dataclass — JSON-serializable, returned to the debug endpoint
# ---------------------------------------------------------------------


@dataclass
class MatchedRecord:
    """One Fiken sent invoice OR credit note that we graduated (or
    attempted to). `record_type` discriminates: 'faktura' or 'kreditnota'.
    """

    fiken_invoice_id: str
    invoice_number: str | None
    issue_date: str | None
    net_nok: float | None
    match_strategy: str  # "draft_uuid" | "reference.our" | "invoice_text" | "associated_invoice" | "no_match"
    record_type: str = "faktura"  # "faktura" | "kreditnota"
    faktura_db_page_id: str | None = None
    faktura_db_cached: bool = False
    oppgave_statuses_set: int = 0
    project_status_set: bool = False
    error: str | None = None


@dataclass
class GraduationSummary:
    project_page_id: str
    project_title: str | None
    company_slug: str
    fiken_invoices_scanned: int = 0
    credit_notes_scanned: int = 0
    matched: list[MatchedRecord] = field(default_factory=list)
    skipped_already_graduated: int = 0
    skipped_no_match: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_page_id": self.project_page_id,
            "project_title": self.project_title,
            "company_slug": self.company_slug,
            "fiken_invoices_scanned": self.fiken_invoices_scanned,
            "credit_notes_scanned": self.credit_notes_scanned,
            "skipped_already_graduated": self.skipped_already_graduated,
            "skipped_no_match": self.skipped_no_match,
            "error": self.error,
            "matched": [
                {
                    "fiken_invoice_id": m.fiken_invoice_id,
                    "record_type": m.record_type,
                    "invoice_number": m.invoice_number,
                    "issue_date": m.issue_date,
                    "net_nok": m.net_nok,
                    "match_strategy": m.match_strategy,
                    "faktura_db_page_id": m.faktura_db_page_id,
                    "faktura_db_cached": m.faktura_db_cached,
                    "oppgave_statuses_set": m.oppgave_statuses_set,
                    "project_status_set": m.project_status_set,
                    "error": m.error,
                }
                for m in self.matched
            ],
        }


# ---------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------


async def graduate_project(project_page_id: str) -> GraduationSummary:
    """Reconcile every sent Fiken invoice that belongs to this project.

    Operator-triggered (via `POST /debug/fiken/graduate-project`).
    Idempotent at every step:
      - Faktura DB writes deduped by FakturaNotionCache.
      - FikenInvoice.sent_at is the per-row "already graduated"
        marker; rows with sent_at NOT NULL skip the entire flow
        on subsequent runs.
      - Status writes use the existing read-first/skip-if-same
        helpers on the notion client.
    """
    summary = GraduationSummary(
        project_page_id=project_page_id,
        project_title=None,
        company_slug=settings.fiken_company_slug or "",
    )

    if not summary.company_slug:
        summary.error = "FIKEN_COMPANY_SLUG not set"
        return summary

    # ---- 1. Read the project page ----
    try:
        project_page = await notion_client.get_page(project_page_id)
    except Exception as err:  # noqa: BLE001
        summary.error = f"Failed to read project page: {err}"
        return summary

    summary.project_title = (
        notion_client.extract_page_title(project_page) or ""
    ).strip() or None

    # ---- 2. List all sent Fiken invoices ----
    try:
        sent_invoices = await fiken_client.list_sent_invoices(
            summary.company_slug
        )
    except Exception as err:  # noqa: BLE001
        summary.error = f"Fiken list_sent_invoices failed: {err}"
        return summary
    summary.fiken_invoices_scanned = len(sent_invoices)

    # ---- 3. Load this project's FikenInvoice rows (for uuid matching
    #         + audit trail per matched invoice) ----
    audit_rows = await _load_audit_rows_for_project(
        summary.company_slug, project_page_id
    )
    # Index by draft_uuid for fast lookup. Rows with no draft_uuid
    # (legacy / poller-discovered) are skipped from this index — they
    # can still be re-matched via reference.our in the fallback path.
    by_draft_uuid: dict[str, FikenInvoice] = {
        row.draft_uuid: row
        for row in audit_rows
        if row.draft_uuid
    }
    # ---- 4. Walk every Fiken invoice; classify + reconcile ----
    project_title_normalized = (
        (summary.project_title or "").strip().casefold()
    )

    for inv in sent_invoices:
        invoice_id = str(inv.get("invoiceId") or "")
        if not invoice_id:
            continue

        # Try draft_uuid match first.
        invoice_draft_uuid = (inv.get("invoiceDraftUuid") or "").strip()
        matched_audit: FikenInvoice | None = (
            by_draft_uuid.get(invoice_draft_uuid)
            if invoice_draft_uuid
            else None
        )

        # Skip rows we've already FULLY graduated for THIS project on a
        # previous run. `sent_at` marks "statuses graduated"; checking it
        # before reconcile prevents re-adding Fakturert beløp on every
        # Sjekk fiken click. Note we check AFTER matching (sent_at lives
        # on the audit row, found via draft_uuid). Fallback-matched
        # invoices (reference.our / invoice_text) have no audit row →
        # re-process is harmless (Faktura DB cache, status skip-if-same).
        #
        # BUT: "graduated statuses" does NOT imply "wrote the Faktura DB
        # row." If a previous run graduated while FAKTURA_DB_ID was unset
        # (C.2 dormant), sent_at got stamped but no Faktura DB row exists.
        # Setting FAKTURA_DB_ID later + re-clicking Sjekk fiken must be
        # able to backfill it. So when the Faktura DB is enabled, only
        # skip if the Faktura DB row ALSO already exists (cache hit);
        # otherwise fall through to _reconcile_one, which writes the
        # missing row and re-runs status writes idempotently (skip-if-
        # same makes the status side a no-op, and _mark_invoice_sent is
        # already set).
        # `faktura_db_backfill_only` = statuses were already graduated on
        # a prior run (sent_at set), but the Faktura DB row is missing
        # (e.g. that run executed while FAKTURA_DB_ID was unset). In that
        # case we re-enter reconcile to write ONLY the Faktura DB row —
        # status graduation must be SKIPPED because _graduate_statuses
        # ADDS to Fakturert beløp with no dedup and would double-count.
        faktura_db_backfill_only = False
        if matched_audit is not None and matched_audit.sent_at is not None:
            faktura_db_row_missing = False
            if settings.faktura_db_id:
                cached = await notion_faktura_db._get_cached_page_id(
                    summary.company_slug, "faktura", invoice_id
                )
                faktura_db_row_missing = cached is None
            if faktura_db_row_missing:
                faktura_db_backfill_only = True
            else:
                summary.skipped_already_graduated += 1
                continue
        match_strategy: str
        if matched_audit is not None:
            match_strategy = "draft_uuid"
        else:
            # Fallback 1: reference.our (Vår referanse) casefold-exact
            # against project title. Cleanest match for invoices the
            # CEO created manually in Fiken's UI WITH a Vår referanse
            # set to the project name.
            reference = inv.get("reference") or {}
            ref_our = (
                reference.get("our")
                or inv.get("ourReference")
                or ""
            ).strip().casefold()
            # Fallback 2: invoiceText (the "Kommentar" field) casefold-
            # exact against project title. Operationally useful when
            # the CEO doesn't fill in Vår referanse but does write a
            # meaningful comment on the invoice — name the Notion
            # project the same string and Sjekk fiken pulls it in.
            invoice_text = (
                inv.get("invoiceText") or ""
            ).strip().casefold()
            if (
                project_title_normalized
                and ref_our
                and ref_our == project_title_normalized
            ):
                match_strategy = "reference.our"
            elif (
                project_title_normalized
                and invoice_text
                and invoice_text == project_title_normalized
            ):
                match_strategy = "invoice_text"
            else:
                # Not ours — skip silently.
                summary.skipped_no_match += 1
                continue

        # ---- 5. Reconcile this one invoice ----
        record = await _reconcile_one(
            summary=summary,
            fiken_invoice=inv,
            invoice_id=invoice_id,
            match_strategy=match_strategy,
            matched_audit=matched_audit,
            project_page_id=project_page_id,
            project_title=summary.project_title,
            faktura_db_backfill_only=faktura_db_backfill_only,
        )
        summary.matched.append(record)

    # ---- 6. List + reconcile credit notes ----
    try:
        credit_notes = await fiken_client.list_credit_notes(
            summary.company_slug
        )
    except Exception as err:  # noqa: BLE001
        # Best-effort: invoice graduation already succeeded; don't kill
        # the summary if credit-note listing fails.
        logger.warning(
            "graduate_project: list_credit_notes failed for %s: %s",
            summary.company_slug,
            err,
        )
        credit_notes = []
    summary.credit_notes_scanned = len(credit_notes)

    # Build a lookup of every FikenInvoice we know about for this
    # project, by fiken_invoice_id. Used for the primary kreditnota
    # match (associatedInvoiceId → parent invoice → project).
    by_invoice_id: dict[str, FikenInvoice] = {
        row.fiken_invoice_id: row for row in audit_rows
    }
    # ALSO build a lookup of Faktura DB pages we've created for this
    # project's invoices — lets the fallback paths walk back from a
    # kreditnota to "was its parent invoice landed in our Faktura DB?"
    # without re-querying Notion. (Only filled with rows graduated
    # IN THIS RUN; cross-run is fine because the next Sjekk fiken
    # rebuilds the map from `audit_rows` which now has sent_at set.)
    cred_already_graduated: set[str] = set()  # would-be future field
    # Already-graduated kreditnotas: ones whose FakturaNotionCache row
    # exists. We check inside the reconcile via _get_cached_page_id,
    # so no separate set needed here.

    for cn in credit_notes:
        credit_note_id = str(cn.get("creditNoteId") or "")
        if not credit_note_id:
            continue

        # Primary match: `associatedInvoiceId` points at a FikenInvoice
        # row we already know belongs to this project. The kreditnota
        # inherits the project AND the parent audit row.
        associated_id = str(cn.get("associatedInvoiceId") or "")
        parent_audit: FikenInvoice | None = (
            by_invoice_id.get(associated_id) if associated_id else None
        )

        # Match strategies in order:
        #
        # 1. associatedInvoiceId (primary, exact) — only present on
        #    kreditnotas created via Fiken's UI "Krediter" button.
        #    Our Til kreditering flow creates kreditnotas via the
        #    drafts API which does NOT set this field, so this path
        #    fires for CEO-manual kreditnotas only.
        #
        # 2. creditNoteText parse — when the Kommentar matches our
        #    "Kreditnota for faktura {N}" pattern (created by the
        #    Til kreditering flow), parse out N and look up the
        #    parent FikenInvoice by invoice_number. This gives us
        #    the FULL parent_audit so per-Oppgave reversal fires.
        #
        # 3. ourReference (Vår referanse) — project_title match.
        #    Lands the Faktura DB row but does NOT reverse statuses
        #    (no per-Oppgave audit trail to subtract from).
        #
        # 4. creditNoteText exact-equals project title — same as 3.
        cn_match_strategy: str
        if parent_audit is not None:
            cn_match_strategy = "associated_invoice"
        else:
            # Strategy 2: parse parent invoice number from Kommentar.
            cn_text_raw = (cn.get("creditNoteText") or "").strip()
            parsed_parent_no = _parse_parent_invoice_number(cn_text_raw)
            if parsed_parent_no:
                parent_audit = _find_audit_by_invoice_number(
                    audit_rows, parsed_parent_no
                )

            if parent_audit is not None:
                cn_match_strategy = "kommentar_parent"
            else:
                # Strategies 3 + 4: title-based fallbacks.
                cn_our_ref = (
                    cn.get("ourReference") or ""
                ).strip().casefold()
                cn_text = cn_text_raw.casefold()
                if (
                    project_title_normalized
                    and cn_our_ref
                    and cn_our_ref == project_title_normalized
                ):
                    cn_match_strategy = "reference.our"
                elif (
                    project_title_normalized
                    and cn_text
                    and cn_text == project_title_normalized
                ):
                    cn_match_strategy = "invoice_text"
                else:
                    summary.skipped_no_match += 1
                    continue

        cn_record = await _reconcile_one_credit_note(
            summary=summary,
            fiken_credit_note=cn,
            credit_note_id=credit_note_id,
            match_strategy=cn_match_strategy,
            parent_audit=parent_audit,
            project_page_id=project_page_id,
            project_title=summary.project_title,
        )
        summary.matched.append(cn_record)

    # Project-level Kreditert rollup. Fires whenever EVERY Oppgave
    # under this project is `Kreditert` (with `Utgår` treated as
    # opt-out), independent of what this particular Sjekk fiken run
    # changed. Catches the case where the per-kreditnota reversal was
    # cached (so the inline-in-reconcile check would skip) but the
    # Project still shows `Fakturert` from an earlier run.
    #
    # Read-first / skip-if-same on the writer side means this is a
    # cheap no-op when the Project is already `Kreditert`.
    try:
        if await _project_oppgaver_all_kreditert(project_page_id):
            await notion_client.set_project_faktura_status(
                project_page_id, status=FAKTURA_STATUS_KREDITERT
            )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "graduate_project: failed to roll Project %s up to "
            "Kreditert: %s",
            project_page_id,
            err,
        )

    return summary


# ---------------------------------------------------------------------
# Per-invoice reconciliation
# ---------------------------------------------------------------------


async def _reconcile_one(
    *,
    summary: GraduationSummary,
    fiken_invoice: dict[str, Any],
    invoice_id: str,
    match_strategy: str,
    matched_audit: FikenInvoice | None,
    project_page_id: str,
    project_title: str | None,
    faktura_db_backfill_only: bool = False,
) -> MatchedRecord:
    """Land the Faktura DB row + (when matched_audit is set) graduate
    Project + Oppgave statuses. Best-effort: errors land on the record
    so the operator sees the full picture in one response.

    `faktura_db_backfill_only`: the row's statuses were already graduated
    on a prior run (sent_at set) but its Faktura DB row is missing. Write
    ONLY the Faktura DB row; skip status graduation (which ADDS to
    Fakturert beløp without dedup and would double-count) and skip
    re-stamping sent_at (already set).
    """
    record = MatchedRecord(
        fiken_invoice_id=invoice_id,
        invoice_number=(
            str(fiken_invoice.get("invoiceNumber"))
            if fiken_invoice.get("invoiceNumber") is not None
            else None
        ),
        issue_date=fiken_invoice.get("issueDate"),
        net_nok=(
            float(fiken_invoice["net"]) / 100.0
            if fiken_invoice.get("net") is not None
            else None
        ),
        match_strategy=match_strategy,
    )

    # ---- A. Faktura DB row (always) ----
    # Idempotency at write-time: FakturaNotionCache. Detect cache vs
    # fresh-write by checking the cache first.
    cached_before = await notion_faktura_db._get_cached_page_id(
        summary.company_slug, "faktura", invoice_id
    )
    try:
        page_id = await notion_faktura_db.create_faktura_row(
            summary.company_slug,
            fiken_record=fiken_invoice,
            record_type="faktura",
            project_page_id=project_page_id,
            project_title=project_title,
        )
        record.faktura_db_page_id = page_id
        record.faktura_db_cached = (
            cached_before is not None
            and page_id == cached_before
        )
    except Exception as err:  # noqa: BLE001
        record.error = f"Faktura DB write failed: {err}"
        logger.exception(
            "graduate_project: Faktura DB write failed for invoice %s",
            invoice_id,
        )
        # Don't abort — try status writes if we have an audit trail.

    # ---- B. Status graduation (only on draft_uuid match) ----
    # Skipped on a backfill-only re-entry: statuses were already
    # graduated on the prior run; re-running would double-add Fakturert
    # beløp (set_oppgave_billed has no dedup).
    if matched_audit is not None and not faktura_db_backfill_only:
        try:
            statuses_set = await _graduate_statuses(
                matched_audit=matched_audit,
                project_page_id=project_page_id,
            )
            record.oppgave_statuses_set = statuses_set
            record.project_status_set = True
        except Exception as err:  # noqa: BLE001
            existing = record.error or ""
            record.error = (
                existing + f"; status graduation failed: {err}"
                if existing
                else f"status graduation failed: {err}"
            )
            logger.exception(
                "graduate_project: status graduation failed for "
                "invoice %s",
                invoice_id,
            )

    # ---- C. Mark FikenInvoice as graduated ----
    # Only update the audit row when we actually know which row to
    # mark (draft_uuid match). The reference.our case creates a Faktura
    # DB row but doesn't have an audit row to flip; subsequent runs
    # will re-write to the same Faktura DB row (cache hit) and stay
    # idempotent.
    if (
        matched_audit is not None
        and record.error is None
        and not faktura_db_backfill_only
    ):
        try:
            await _mark_invoice_sent(
                company_slug=summary.company_slug,
                fiken_invoice_id=matched_audit.fiken_invoice_id,
                sent_url=_extract_sent_url(fiken_invoice, summary.company_slug),
                invoice_number=record.invoice_number,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "graduate_project: failed to mark FikenInvoice "
                "sent_at for invoice %s: %s",
                invoice_id,
                err,
            )

    return record


async def _reconcile_one_credit_note(
    *,
    summary: GraduationSummary,
    fiken_credit_note: dict[str, Any],
    credit_note_id: str,
    match_strategy: str,
    parent_audit: FikenInvoice | None,
    project_page_id: str,
    project_title: str | None,
) -> MatchedRecord:
    """Land a kreditnota in the Faktura DB. C.3a.x scope: just write
    the row (idempotent via FakturaNotionCache). Status reversal +
    Fakturert beløp subtraction handled in C.3a.y (see _reverse_statuses
    below).

    The Notion writer needs the kreditnota payload to LOOK enough like
    an invoice that the existing FAKTURA_PROPS mapping applies. The
    main shape diffs handled here:
      - `creditNoteId` / `creditNoteNumber` → mapped to invoiceId /
        invoiceNumber by the writer's `_extract_record_id` / number
        helpers when record_type='kreditnota'.
      - `net` is NEGATIVE on credit notes → writer / number conversion
        handles it as-is, Notion shows negative kr.
      - `creditNoteText` → writer reads as Kommentar fallback when
        invoiceText absent.
      - `associatedInvoiceId` → writer looks it up in Postgres to
        populate Kreditnota til faktura (the parent's invoiceNumber).
    """
    # Net comes back as int øre (negative). Convert for the summary.
    net_raw = fiken_credit_note.get("net")
    net_nok = float(net_raw) / 100.0 if net_raw is not None else None

    record = MatchedRecord(
        fiken_invoice_id=credit_note_id,
        record_type="kreditnota",
        invoice_number=(
            str(fiken_credit_note.get("creditNoteNumber"))
            if fiken_credit_note.get("creditNoteNumber") is not None
            else None
        ),
        issue_date=fiken_credit_note.get("issueDate"),
        net_nok=net_nok,
        match_strategy=match_strategy,
    )

    # Resolve the parent invoice's PRINTABLE NUMBER for the
    # `Kreditnota til faktura` column. Two paths:
    #   1. Parent is in our FikenInvoice table (we graduated it
    #      previously) → use parent_audit.invoice_number.
    #   2. Otherwise → GET the parent invoice from Fiken to read
    #      its invoiceNumber. Skipped silently on error; the
    #      column just stays blank.
    parent_invoice_number: int | None = None
    if parent_audit is not None and parent_audit.invoice_number:
        try:
            parent_invoice_number = int(parent_audit.invoice_number)
        except (TypeError, ValueError):
            parent_invoice_number = None
    else:
        associated_id = fiken_credit_note.get("associatedInvoiceId")
        if associated_id:
            try:
                async with await fiken_client._client() as client:
                    resp = await client.get(
                        f"/companies/{summary.company_slug}/invoices/{associated_id}"
                    )
                    if resp.status_code == 200:
                        parent_payload = resp.json()
                        raw_num = parent_payload.get("invoiceNumber")
                        if raw_num is not None:
                            parent_invoice_number = int(raw_num)
            except Exception as err:  # noqa: BLE001
                logger.info(
                    "graduate_project: could not resolve parent "
                    "invoice number for kreditnota %s (associated=%s): %s",
                    credit_note_id,
                    associated_id,
                    err,
                )

    # Faktura DB row. Cache key is (company_slug, "kreditnota", id).
    cached_before = await notion_faktura_db._get_cached_page_id(
        summary.company_slug, "kreditnota", credit_note_id
    )
    try:
        page_id = await notion_faktura_db.create_faktura_row(
            summary.company_slug,
            fiken_record=fiken_credit_note,
            record_type="kreditnota",
            project_page_id=project_page_id,
            project_title=project_title,
            parent_invoice_number=parent_invoice_number,
        )
        record.faktura_db_page_id = page_id
        record.faktura_db_cached = (
            cached_before is not None and page_id == cached_before
        )
    except Exception as err:  # noqa: BLE001
        record.error = f"Faktura DB write failed: {err}"
        logger.exception(
            "graduate_project: Faktura DB write failed for kreditnota %s",
            credit_note_id,
        )
        return record

    # Status + Fakturert beløp reversal (C.3a.y).
    # Only fires when:
    #   1. The Faktura DB cache MISSED (faktura_db_cached=False) —
    #      a cache hit means we already processed this kreditnota on
    #      a prior Sjekk fiken run. Re-running the reversal would
    #      double-subtract from Fakturert beløp.
    #   2. We have a parent audit row (so we know which Oppgaver were
    #      on the original faktura via FikenInvoiceLine).
    #   3. The kreditnota's net (absolute value) matches the parent's
    #      net within a tolerance — i.e. it's a FULL reversal, not a
    #      partial credit. Partial credits leave the statuses alone
    #      (operator handles them manually).
    if (
        parent_audit is not None
        and not record.faktura_db_cached
    ):
        try:
            await _maybe_reverse_for_kreditnota(
                summary=summary,
                fiken_credit_note=fiken_credit_note,
                parent_audit=parent_audit,
                project_page_id=project_page_id,
                record=record,
            )
        except Exception as err:  # noqa: BLE001
            existing = record.error or ""
            note = f"reversal failed: {err}"
            record.error = (
                existing + "; " + note if existing else note
            )
            logger.exception(
                "graduate_project: status reversal failed for "
                "kreditnota %s",
                credit_note_id,
            )

    return record


async def _maybe_reverse_for_kreditnota(
    *,
    summary: GraduationSummary,
    fiken_credit_note: dict[str, Any],
    parent_audit: FikenInvoice,
    project_page_id: str,
    record: MatchedRecord,
) -> None:
    """If the kreditnota fully reverses its parent faktura, subtract
    each per-Oppgave NOK from `Fakturert beløp` and flip statuses to
    `Kreditert` on both the Oppgaver and the Project (the project-
    level write fires only when ALL the project's sent fakturas are
    fully credited).

    Partial credits (kreditnota net < parent net) leave everything
    alone — too ambiguous to auto-reverse correctly. Operator handles
    via manual Notion edits.
    """
    # ---- 1. Sum the parent's audit lines to get its expected net ----
    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine.billed_amount_ore).where(
            FikenInvoiceLine.company_slug == parent_audit.company_slug,
            FikenInvoiceLine.fiken_invoice_id
            == parent_audit.fiken_invoice_id,
        )
        parent_line_amounts = list(
            (await session.execute(stmt)).scalars()
        )
    parent_net_ore = sum(int(a or 0) for a in parent_line_amounts)
    kreditnota_net_ore = abs(int(fiken_credit_note.get("net") or 0))

    # ---- 2. Verify full reversal (tolerate 1 øre rounding) ----
    if abs(parent_net_ore - kreditnota_net_ore) > 1:
        logger.info(
            "graduate_project: kreditnota %s is a PARTIAL credit "
            "(kreditnota_net=%d øre, parent_net=%d øre) — leaving "
            "statuses alone, operator handles manually.",
            record.fiken_invoice_id,
            kreditnota_net_ore,
            parent_net_ore,
        )
        return

    # ---- 3. Per-Oppgave reversal ----
    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine).where(
            FikenInvoiceLine.company_slug == parent_audit.company_slug,
            FikenInvoiceLine.fiken_invoice_id
            == parent_audit.fiken_invoice_id,
        )
        lines = list((await session.execute(stmt)).scalars())

    oppgave_count = 0
    for line in lines:
        if not line.oppgave_page_id:
            continue
        try:
            page = await notion_client.get_page(line.oppgave_page_id)
            current_nok = (
                notion_client.read_number_prop(
                    page, OPPGAVER_PROPS["billed_amount"]
                )
                or 0.0
            )
            new_total = current_nok - (line.billed_amount_ore / 100.0)
            # Clamp to 0 (defensive: if other graduations already
            # changed things, don't push negative).
            if new_total < 0:
                new_total = 0.0
            await notion_client.set_oppgave_billed(
                line.oppgave_page_id,
                status=FAKTURERT_STATUS_KREDITERT,
                billed_amount_nok=new_total,
            )
            oppgave_count += 1
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "graduate_project: failed to reverse Oppgave %s for "
                "kreditnota %s: %s",
                line.oppgave_page_id,
                record.fiken_invoice_id,
                err,
            )
    record.oppgave_statuses_set = oppgave_count
    # Project-level Kreditert rollup happens at the END of
    # graduate_project (not here) — so it fires even when a Sjekk
    # fiken run is otherwise a full cache hit. See `graduate_project`'s
    # tail.


# ---------------------------------------------------------------------
# Status graduation
# ---------------------------------------------------------------------


async def _graduate_statuses(
    *, matched_audit: FikenInvoice, project_page_id: str
) -> int:
    """For each Oppgave that was on this now-sent invoice: ADD the
    audit-line NOK to Notion's `Fakturert beløp` (so the rollup-driven
    `Fakturert sum` on the Project always reflects actually-invoiced
    money, not draft commitments) AND flip `Fakturert status` to the
    terminal history value. Then write the Project's `Faktura status`
    and clear the draft URL.

    Reads `FikenInvoiceLine` audit rows to know which Oppgaver to
    touch + how much to add. invoice_type ("oppstart" / "slutt") on
    the FikenInvoice row decides which history option to write.

    Returns the count of Oppgave rows that got status-set.
    """
    invoice_type = matched_audit.invoice_type or "slutt"
    oppgave_history = (
        FAKTURERT_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURERT_STATUS_FULL
    )
    project_history = (
        FAKTURA_STATUS_50
        if invoice_type == "oppstart"
        else FAKTURA_STATUS_FULL
    )

    # Load the audit lines.
    async with SessionLocal() as session:
        stmt = select(FikenInvoiceLine).where(
            FikenInvoiceLine.company_slug == matched_audit.company_slug,
            FikenInvoiceLine.fiken_invoice_id
            == matched_audit.fiken_invoice_id,
        )
        lines = list((await session.execute(stmt)).scalars())

    oppgave_count = 0
    for line in lines:
        if not line.oppgave_page_id:
            continue
        # Read CURRENT Fakturert beløp on the Oppgave, ADD this
        # invoice's billed NOK to it, write the new total. Read-first
        # so a graduation of an oppstart invoice doesn't blow away a
        # prior slutt write (shouldn't happen given the eligibility
        # gate, but defensive). Idempotency: if this graduation has
        # already run, the audit-line amount has already been added —
        # but Notion's set_oppgave_billed has no inherent dedup, so
        # we'd double-add on re-run. The C.3 cache (sent_at on the
        # FikenInvoice) prevents re-running the same graduation; the
        # debug endpoint passes audit-row state explicitly.
        line_nok = line.billed_amount_ore / 100.0
        try:
            page = await notion_client.get_page(line.oppgave_page_id)
            current_nok = (
                notion_client.read_number_prop(
                    page, OPPGAVER_PROPS["billed_amount"]
                )
                or 0.0
            )
            new_total = current_nok + line_nok
            await notion_client.set_oppgave_billed(
                line.oppgave_page_id,
                status=oppgave_history,
                billed_amount_nok=new_total,
            )
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "graduate_project: failed to set Fakturert status + "
                "beløp on Oppgave %s: %s",
                line.oppgave_page_id,
                err,
            )
            continue
        oppgave_count += 1

    # Project-level Faktura status: write the terminal history value.
    try:
        await notion_client.set_project_faktura_status(
            project_page_id, status=project_history
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "graduate_project: failed to set project %s Faktura "
            "status to %r: %s",
            project_page_id,
            project_history,
            err,
        )

    # Clear the Faktura utkast URL on the project. Once the draft is
    # sent, the draft URL goes 404 in Fiken anyway; clearing it removes
    # a misleading clickable link. The Faktura DB row's own URL takes
    # over as "here is the invoice."
    try:
        await notion_client.set_project_draft_url(
            project_page_id, url=None
        )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "graduate_project: failed to clear Faktura utkast on "
            "project %s: %s",
            project_page_id,
            err,
        )

    return oppgave_count


# ---------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------


async def _load_audit_rows_for_project(
    company_slug: str, project_page_id: str
) -> list[FikenInvoice]:
    """All FikenInvoice rows for this project. Includes already-graduated
    rows so the matching pass can short-circuit on them."""
    async with SessionLocal() as session:
        stmt = select(FikenInvoice).where(
            FikenInvoice.company_slug == company_slug,
            FikenInvoice.project_page_id == project_page_id,
        )
        return list((await session.execute(stmt)).scalars())


async def _mark_invoice_sent(
    *,
    company_slug: str,
    fiken_invoice_id: str,
    sent_url: str | None,
    invoice_number: str | None,
) -> None:
    """UPSERT sent_at / sent_url / invoice_number on the audit row.
    Uses pg_insert + on_conflict_do_update so a future poller can
    safely re-mark (or freshly discover the row, though this code
    path only fires when matched_audit is non-None)."""
    async with SessionLocal() as session:
        now = datetime.now(timezone.utc)
        stmt = (
            pg_insert(FikenInvoice)
            .values(
                company_slug=company_slug,
                fiken_invoice_id=fiken_invoice_id,
                sent_at=now,
                sent_url=sent_url,
                invoice_number=invoice_number,
            )
            .on_conflict_do_update(
                index_elements=["company_slug", "fiken_invoice_id"],
                set_={
                    "sent_at": now,
                    "sent_url": sent_url,
                    "invoice_number": invoice_number,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()


# Matches our Til kreditering flow's Kommentar pattern:
#   "Kreditnota for faktura 10085"
# Captures the integer invoice number for parent lookup.
import re as _re

_PARENT_INVOICE_FROM_KOMMENTAR = _re.compile(
    r"kreditnota\s+for\s+faktura\s+(\d+)", _re.IGNORECASE
)


async def _project_oppgaver_all_kreditert(
    project_page_id: str,
) -> bool:
    """True iff every Oppgave under this project is either `Kreditert`
    or `Utgår` (operator opt-out), AND at least one is `Kreditert`.

    The "at least one Kreditert" guard prevents firing on a project
    that has only Utgår rows (or, theoretically, no Oppgaver at all
    — though that case shouldn't arise for a real project that's
    been invoiced).

    Reads fresh from Notion so changes the caller just made to
    individual Oppgaver are visible. Korreksjonsrunde sub-rows are
    skipped since they're never invoiced.
    """
    try:
        oppgaver = await notion_client.oppgaver_for_project(
            project_page_id
        )
    except Exception as err:  # noqa: BLE001
        logger.info(
            "graduate_project: oppgaver_for_project failed during "
            "Kreditert rollup check (%s) — skipping rollup.",
            err,
        )
        return False

    from gb_automations.config import (
        FAKTURERT_STATUS_KREDITERT as _KRED,
        FAKTURERT_STATUS_UTGAR as _UTGAR,
        KORREKSJON_KIND_KORREKSJONSRUNDE as _KORREKSJON,
        OPPGAVER_PROPS as _OPPS,
    )

    saw_kreditert = False
    for row in oppgaver:
        raw_type = (
            notion_client.task_discipline(row) or ""
        ).strip().lower()
        if raw_type == _KORREKSJON.lower():
            continue
        # Zero/missing-Pris rows are never invoiced (filtered by
        # _eligible_rows), so they never reach Kreditert/Utgår. Ignore
        # them here too — exactly like Korreksjonsrunde — otherwise a
        # single unpriced row would permanently block the all-Kreditert
        # project rollup.
        price = notion_client.read_number_prop(row, _OPPS["price_per_row"])
        if price is None or price <= 0:
            continue
        status = (
            notion_client.read_select_name(row, _OPPS["billed_status"])
            or ""
        )
        if status == _KRED:
            saw_kreditert = True
        elif status == _UTGAR:
            continue
        else:
            # Some Oppgave is NOT Kreditert/Utgår — project isn't
            # fully credited.
            return False
    return saw_kreditert


def _parse_parent_invoice_number(kommentar: str) -> str | None:
    """Parse the parent invoice number out of a kreditnota Kommentar
    that matches the Til kreditering flow's pattern.

    Returns the number as a string (matches FikenInvoice.invoice_number
    which is stored as VARCHAR), or None when the Kommentar doesn't
    match the pattern.
    """
    if not kommentar:
        return None
    m = _PARENT_INVOICE_FROM_KOMMENTAR.search(kommentar)
    return m.group(1) if m else None


def _find_audit_by_invoice_number(
    audit_rows: list[FikenInvoice], invoice_number: str
) -> FikenInvoice | None:
    """Linear scan of this project's audit rows for the parent with
    the matching invoice_number. Cheap — most projects have ≤ 2 sent
    invoices."""
    for row in audit_rows:
        if row.invoice_number and str(row.invoice_number) == str(
            invoice_number
        ):
            return row
    return None


def _extract_sent_url(
    fiken_invoice: dict[str, Any], company_slug: str
) -> str | None:
    """Compose the Fiken UI URL for a sent invoice or credit note.

    Empirically pinned (2026-06-21): the UI path is
        https://fiken.no/foretak/{slug}/handel/salg/{saleId}
    for BOTH invoices and credit notes. `saleId` lives on the
    `sale.saleId` block of Fiken's payload. The earlier guesses
    (`/faktura/{invoiceId}`, `/kreditnota/{creditNoteId}`) returned
    blank pages.

    Returns None if either slug or saleId is missing. Mirrors
    notion_faktura_db._build_url; kept as a separate function so this
    module doesn't import the private helper.
    """
    sale = fiken_invoice.get("sale") or {}
    sale_id = sale.get("saleId")
    if not company_slug or sale_id is None:
        return None
    return (
        f"https://fiken.no/foretak/{company_slug}/handel/salg/{sale_id}"
    )
