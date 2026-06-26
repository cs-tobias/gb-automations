"""Tests for Phase C.3a graduate_project engine.

The hot path is integration code (Notion + Postgres + Fiken). These
tests focus on the parts where bugs would silently misroute:

1. `_extract_sent_url` pure helper.
2. `MatchedRecord` / `GraduationSummary` dict serialization (the
   debug endpoint returns it as JSON; shape must match docs).
3. End-to-end match classification through `graduate_project` with
   stubbed boundaries. Catches "did I name the field right?" /
   "does the casefold compare survive a Norwegian title?" bugs
   without needing live Fiken.

The DB cache + status write paths are exercised by smoke tests
against the running container in CI.h.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from gb_automations.sync import graduate_project as engine


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------


def test_extract_sent_url_invoice():
    """Pinned empirically (2026-06-21): Fiken's UI uses /handel/salg/
    {saleId} for both invoices and kreditnotas."""
    fiken_invoice = {
        "invoiceId": 4493258455,
        "sale": {"saleId": 4493497377},
    }
    url = engine._extract_sent_url(fiken_invoice, "goldbox-as")
    assert (
        url
        == "https://fiken.no/foretak/goldbox-as/handel/salg/4493497377"
    )


def test_extract_sent_url_returns_None_when_slug_missing():
    assert (
        engine._extract_sent_url(
            {"sale": {"saleId": 1}}, ""
        )
        is None
    )


def test_extract_sent_url_returns_None_when_sale_id_missing():
    assert engine._extract_sent_url({}, "goldbox-as") is None
    assert engine._extract_sent_url({"sale": {}}, "goldbox-as") is None


# ---------------------------------------------------------------------
# Result serialization
# ---------------------------------------------------------------------


def test_summary_as_dict_empty():
    summary = engine.GraduationSummary(
        project_page_id="p1",
        project_title="Test",
        company_slug="goldbox-as",
    )
    d = summary.as_dict()
    assert d["project_page_id"] == "p1"
    assert d["project_title"] == "Test"
    assert d["company_slug"] == "goldbox-as"
    assert d["fiken_invoices_scanned"] == 0
    assert d["matched"] == []
    assert d["skipped_already_graduated"] == 0
    assert d["skipped_no_match"] == 0
    assert d["error"] is None


def test_summary_as_dict_with_matched_record():
    summary = engine.GraduationSummary(
        project_page_id="p1",
        project_title="Test",
        company_slug="goldbox-as",
        fiken_invoices_scanned=3,
    )
    summary.matched.append(
        engine.MatchedRecord(
            fiken_invoice_id="4493258455",
            invoice_number="10051",
            issue_date="2026-06-18",
            net_nok=6240.0,
            match_strategy="draft_uuid",
            faktura_db_page_id="notion-page-1",
            faktura_db_cached=False,
            oppgave_statuses_set=3,
            project_status_set=True,
        )
    )
    d = summary.as_dict()
    assert len(d["matched"]) == 1
    record = d["matched"][0]
    assert record["fiken_invoice_id"] == "4493258455"
    assert record["match_strategy"] == "draft_uuid"
    assert record["oppgave_statuses_set"] == 3
    assert record["project_status_set"] is True


# ---------------------------------------------------------------------
# End-to-end match classification
# ---------------------------------------------------------------------


def _make_fiken_invoice(
    *,
    invoice_id: int = 4493258455,
    invoice_number: int = 10051,
    invoice_draft_uuid: str | None = None,
    reference_our: str | None = None,
    invoice_text: str | None = None,
    issue_date: str = "2026-06-18",
    net: int = 624000,
) -> dict[str, Any]:
    """Minimal sent-invoice payload for tests."""
    inv: dict[str, Any] = {
        "invoiceId": invoice_id,
        "invoiceNumber": invoice_number,
        "issueDate": issue_date,
        "net": net,
    }
    if invoice_draft_uuid:
        inv["invoiceDraftUuid"] = invoice_draft_uuid
    if reference_our is not None:
        inv["reference"] = {"our": reference_our}
    if invoice_text is not None:
        inv["invoiceText"] = invoice_text
    return inv


class _StubFikenInvoice:
    """In-memory stand-in for the FikenInvoice ORM row. The engine
    only reads draft_uuid, sent_at, fiken_invoice_id, project_page_id,
    invoice_type, company_slug — nothing else is touched."""

    def __init__(
        self,
        *,
        fiken_invoice_id: str,
        draft_uuid: str | None,
        sent_at: Any = None,
        invoice_type: str = "oppstart",
        company_slug: str = "goldbox-as",
        project_page_id: str = "p1",
    ):
        self.fiken_invoice_id = fiken_invoice_id
        self.draft_uuid = draft_uuid
        self.sent_at = sent_at
        self.invoice_type = invoice_type
        self.company_slug = company_slug
        self.project_page_id = project_page_id


def _make_project_page(*, page_id: str = "p1", title: str = "0001_Test") -> dict[str, Any]:
    return {
        "id": page_id,
        "properties": {
            "Navn": {
                "type": "title",
                "title": [{"plain_text": title}],
            }
        },
    }


@pytest.mark.asyncio
async def test_graduate_project_matches_by_draft_uuid(
    monkeypatch: pytest.MonkeyPatch,
):
    """Primary match path: invoice carries our stored draft_uuid →
    matches on the audit row → status graduation fires."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="0001_Test")
    sent_invoices = [
        _make_fiken_invoice(
            invoice_draft_uuid="uuid-A",
            reference_our="something-else",
        )
    ]
    audit_rows = [
        _StubFikenInvoice(
            fiken_invoice_id="draft-1",
            draft_uuid="uuid-A",
            invoice_type="oppstart",
            project_page_id="p1",
        )
    ]

    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    monkeypatch.setattr(
        engine,
        "_reconcile_one",
        AsyncMock(
            return_value=engine.MatchedRecord(
                fiken_invoice_id="4493258455",
                invoice_number="10051",
                issue_date="2026-06-18",
                net_nok=6240.0,
                match_strategy="draft_uuid",
            )
        ),
    )

    summary = await engine.graduate_project("p1")
    assert summary.fiken_invoices_scanned == 1
    assert len(summary.matched) == 1
    assert summary.matched[0].match_strategy == "draft_uuid"
    assert summary.skipped_no_match == 0
    assert summary.skipped_already_graduated == 0
    # _reconcile_one was called with the matched audit row.
    call_kwargs = engine._reconcile_one.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["match_strategy"] == "draft_uuid"
    assert call_kwargs["matched_audit"] is audit_rows[0]


@pytest.mark.asyncio
async def test_graduate_project_falls_back_to_reference_our(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fallback: no draft_uuid match, but reference.our exact-matches
    project title (casefold + strip) → fires with match_strategy =
    'reference.our' and matched_audit=None."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="0001_Test")
    # NB: title in CAPS + extra whitespace; reference.our should still match
    # via casefold + strip.
    sent_invoices = [
        _make_fiken_invoice(
            invoice_draft_uuid="uuid-Z",  # nobody's audit row
            reference_our="  0001_test  ",
        )
    ]
    audit_rows = [
        _StubFikenInvoice(
            fiken_invoice_id="draft-1",
            draft_uuid="uuid-OTHER",
            project_page_id="p1",
        )
    ]

    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    monkeypatch.setattr(
        engine,
        "_reconcile_one",
        AsyncMock(
            return_value=engine.MatchedRecord(
                fiken_invoice_id="4493258455",
                invoice_number="10051",
                issue_date="2026-06-18",
                net_nok=6240.0,
                match_strategy="reference.our",
            )
        ),
    )

    summary = await engine.graduate_project("p1")
    assert len(summary.matched) == 1
    assert summary.matched[0].match_strategy == "reference.our"
    call_kwargs = engine._reconcile_one.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["match_strategy"] == "reference.our"
    assert call_kwargs["matched_audit"] is None


@pytest.mark.asyncio
async def test_graduate_project_falls_back_to_invoice_text(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fallback 2: no draft_uuid match, no reference.our match, but
    invoiceText (Kommentar) exact-matches the project title → fires
    with match_strategy = 'invoice_text'. Operational anchor for the
    common case where the CEO writes a meaningful Kommentar but skips
    Vår referanse.
    """
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="Sony ZV-E1 Kamera DEL 3")
    # No matching draft_uuid, no reference.our — but invoiceText
    # exactly matches the project title (with extra whitespace + case
    # variation to confirm casefold + strip).
    sent_invoices = [
        _make_fiken_invoice(
            invoice_draft_uuid="uuid-Z",
            reference_our=None,
            invoice_text="  sony zv-e1 kamera del 3  ",
        )
    ]
    audit_rows: list[_StubFikenInvoice] = []

    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    monkeypatch.setattr(
        engine,
        "_reconcile_one",
        AsyncMock(
            return_value=engine.MatchedRecord(
                fiken_invoice_id="4493258455",
                invoice_number="10051",
                issue_date="2026-06-18",
                net_nok=6240.0,
                match_strategy="invoice_text",
            )
        ),
    )

    summary = await engine.graduate_project("p1")
    assert len(summary.matched) == 1
    assert summary.matched[0].match_strategy == "invoice_text"
    call_kwargs = engine._reconcile_one.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["match_strategy"] == "invoice_text"
    assert call_kwargs["matched_audit"] is None


@pytest.mark.asyncio
async def test_graduate_project_reference_our_wins_over_invoice_text(
    monkeypatch: pytest.MonkeyPatch,
):
    """If BOTH reference.our and invoiceText match, reference.our
    takes precedence — it's the canonical anchor; invoiceText is a
    weaker fallback.
    """
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="0001_Test")
    sent_invoices = [
        _make_fiken_invoice(
            invoice_draft_uuid="uuid-Z",
            reference_our="0001_Test",
            invoice_text="0001_Test",
        )
    ]
    audit_rows: list[_StubFikenInvoice] = []

    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    monkeypatch.setattr(
        engine,
        "_reconcile_one",
        AsyncMock(
            return_value=engine.MatchedRecord(
                fiken_invoice_id="4493258455",
                invoice_number="10051",
                issue_date="2026-06-18",
                net_nok=6240.0,
                match_strategy="reference.our",
            )
        ),
    )

    await engine.graduate_project("p1")
    call_kwargs = engine._reconcile_one.call_args.kwargs  # type: ignore[attr-defined]
    assert call_kwargs["match_strategy"] == "reference.our"


@pytest.mark.asyncio
async def test_graduate_project_no_match_increments_skipped_counter(
    monkeypatch: pytest.MonkeyPatch,
):
    """Both match strategies miss → invoice silently skipped, counter
    incremented, _reconcile_one NOT called."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="0001_Test")
    sent_invoices = [
        _make_fiken_invoice(
            invoice_draft_uuid="uuid-X",
            reference_our="some-other-project",
        )
    ]
    audit_rows: list[_StubFikenInvoice] = []

    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(engine, "_reconcile_one", reconcile)

    summary = await engine.graduate_project("p1")
    assert summary.fiken_invoices_scanned == 1
    assert summary.skipped_no_match == 1
    assert summary.matched == []
    reconcile.assert_not_called()


def _setup_already_graduated(monkeypatch: pytest.MonkeyPatch):
    """Shared fixture: one sent invoice whose audit row is already
    graduated (sent_at set). Returns the reconcile mock for assertions.
    """
    from datetime import datetime, timezone

    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")

    project_page = _make_project_page(title="0001_Test")
    sent_invoices = [_make_fiken_invoice(invoice_draft_uuid="uuid-A")]
    audit_rows = [
        _StubFikenInvoice(
            fiken_invoice_id="4493258455",
            draft_uuid="uuid-A",
            sent_at=datetime.now(timezone.utc),  # already graduated
            project_page_id="p1",
        )
    ]
    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(return_value=sent_invoices),
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_credit_notes",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        engine,
        "_load_audit_rows_for_project",
        AsyncMock(return_value=audit_rows),
    )
    reconcile = AsyncMock()
    monkeypatch.setattr(engine, "_reconcile_one", reconcile)
    return reconcile


@pytest.mark.asyncio
async def test_graduate_project_skips_when_faktura_db_row_exists(
    monkeypatch: pytest.MonkeyPatch,
):
    """sent_at set AND Faktura DB row already cached → fully done, skip
    without re-calling _reconcile_one."""
    from gb_automations.config import settings

    reconcile = _setup_already_graduated(monkeypatch)
    monkeypatch.setattr(settings, "faktura_db_id", "fakturadb")
    # Cache HIT — the Faktura DB row exists.
    monkeypatch.setattr(
        engine.notion_faktura_db,
        "_get_cached_page_id",
        AsyncMock(return_value="existing-faktura-page"),
    )

    summary = await engine.graduate_project("p1")
    assert summary.skipped_already_graduated == 1
    reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_graduate_project_skips_when_faktura_db_disabled(
    monkeypatch: pytest.MonkeyPatch,
):
    """sent_at set + FAKTURA_DB_ID unset → nothing to backfill, skip
    (legacy behavior preserved)."""
    from gb_automations.config import settings

    reconcile = _setup_already_graduated(monkeypatch)
    monkeypatch.setattr(settings, "faktura_db_id", "")

    summary = await engine.graduate_project("p1")
    assert summary.skipped_already_graduated == 1
    reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_graduate_project_backfills_missing_faktura_db_row(
    monkeypatch: pytest.MonkeyPatch,
):
    """sent_at set (statuses already graduated) BUT Faktura DB row is
    missing (e.g. prior run had FAKTURA_DB_ID unset) → re-enter
    _reconcile_one in backfill-only mode to write the missing row, with
    status graduation suppressed so Fakturert beløp isn't double-added.
    """
    from gb_automations.config import settings

    reconcile = _setup_already_graduated(monkeypatch)
    monkeypatch.setattr(settings, "faktura_db_id", "fakturadb")
    # Cache MISS — the Faktura DB row was never written.
    monkeypatch.setattr(
        engine.notion_faktura_db,
        "_get_cached_page_id",
        AsyncMock(return_value=None),
    )

    summary = await engine.graduate_project("p1")
    # Not counted as skipped — we re-process it.
    assert summary.skipped_already_graduated == 0
    reconcile.assert_called_once()
    # And it's called in backfill-only mode (statuses must NOT re-run).
    assert reconcile.call_args.kwargs["faktura_db_backfill_only"] is True


@pytest.mark.asyncio
async def test_graduate_project_errors_when_no_company_slug(
    monkeypatch: pytest.MonkeyPatch,
):
    """Defensive: a blank FIKEN_COMPANY_SLUG returns an error
    immediately rather than trying to call Fiken."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "")
    summary = await engine.graduate_project("p1")
    assert summary.error == "FIKEN_COMPANY_SLUG not set"
    assert summary.fiken_invoices_scanned == 0


@pytest.mark.asyncio
async def test_graduate_project_records_error_on_fiken_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    """list_sent_invoices raising → error captured on summary."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")
    project_page = _make_project_page(title="0001_Test")
    monkeypatch.setattr(
        engine.notion_client, "get_page", AsyncMock(return_value=project_page)
    )
    monkeypatch.setattr(
        engine.fiken_client,
        "list_sent_invoices",
        AsyncMock(side_effect=RuntimeError("Fiken 503")),
    )

    summary = await engine.graduate_project("p1")
    assert summary.error is not None
    assert "Fiken 503" in summary.error
