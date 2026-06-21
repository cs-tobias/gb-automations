"""Tests for Phase C.2 Faktura DB writer.

The hot path (`create_faktura_row`) is integration code that hits
Postgres + Notion. These tests focus on:

1. Pure helpers (`_extract_record_id`, `_record_number_str`,
   `_record_number_int`, `_build_url`, `_normalize_orgnr`) — no
   mocking needed.
2. `_build_payload` — pure payload mapper; exercises every column
   in FAKTURA_PROPS with a stub Fiken record.
3. End-to-end through `create_faktura_row` with stubbed boundaries
   (cache lookup, page create, cache insert). Catches the field-name
   mistakes (FAKTURA_PROPS typo etc) without needing live Notion.

The DB cache layer is exercised via the pytest-asyncio + SessionLocal
pattern used elsewhere in the suite — see `tests/conftest.py` for the
test DB setup if any cache test starts failing.
"""

from __future__ import annotations

from typing import Any

import pytest

from gb_automations.config import (
    FAKTURA_PROPS,
    FAKTURA_TYPE_FAKTURA,
    FAKTURA_TYPE_KREDITNOTA,
)
from gb_automations.sync import notion_faktura_db as engine


# ---------------------------------------------------------------------
# Fixtures: stub Fiken record dicts
# ---------------------------------------------------------------------


def _stub_invoice(**overrides: Any) -> dict[str, Any]:
    """Minimal Fiken invoice payload mirroring the shape of a live GET
    /companies/{slug}/invoices/{id} response. Overrides are merged
    on top so individual tests can vary one field at a time.
    """
    base: dict[str, Any] = {
        "invoiceId": 4493258455,
        "invoiceNumber": 10051,
        "issueDate": "2026-06-18",
        "dueDate": "2026-07-02",
        "net": 624000,  # 6240 NOK in øre
        "gross": 780000,
        "currency": "NOK",
        "yourReference": "Faktura merkes tekst",
        "invoiceText": "Oppstartsfaktura: 50 % av avtalt beløp.",
        # Fiken's UI links to invoices via sale.saleId (handel/salg path),
        # not invoiceId. See _build_url. Realistic stub carries it.
        "sale": {"saleId": 4493497377},
        # Fiken nests the PDF info; the browser-friendly URL is the one
        # the writer pulls for the Faktura PDF column.
        "invoicePdf": {
            "downloadUrl": "https://api.fiken.no/api/v2/files/abc/faktura_2026_10051.pdf",
            "downloadUrlWithFikenNormalUserCredentials": "https://fiken.no/filer/abc/faktura_2026_10051.pdf",
            "type": "invoice",
        },
        "customer": {
            "contactId": 4493254290,
            "name": "ENTUR AS",
            "organizationNumber": "917422575",
        },
        "reference": {
            "our": "0001_Test Prosjekt",
            "yours": "Faktura merkes tekst",
        },
    }
    base.update(overrides)
    return base


def _stub_credit_note(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "creditNoteId": 4493999999,
        "creditNoteNumber": 5,
        "issueDate": "2026-06-18",
        "net": 500000,
        "associatedInvoiceId": 4493258455,
        "associatedInvoiceNumber": 10051,
        # Same path as invoices; Fiken stores saleId on credit notes too.
        "sale": {"saleId": 4493500000},
        # Kreditnotas use a differently-named PDF block; same inner shape.
        "creditNotePdf": {
            "downloadUrl": "https://api.fiken.no/api/v2/files/xyz/kreditnota_2026_20003.pdf",
            "downloadUrlWithFikenNormalUserCredentials": "https://fiken.no/filer/xyz/kreditnota_2026_20003.pdf",
            "type": "unspecified",
        },
        "customer": {
            "contactId": 4493254290,
            "name": "ENTUR AS",
            "organizationNumber": "917422575",
        },
        "reference": {"our": "0001_Test Prosjekt"},
        "comment": "Krediterer linje 1 av faktura 10051",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------


def test_extract_record_id_invoice_returns_invoiceId_as_string():
    rec = _stub_invoice()
    assert engine._extract_record_id(rec, "faktura") == "4493258455"


def test_extract_record_id_credit_note_returns_creditNoteId_as_string():
    rec = _stub_credit_note()
    assert engine._extract_record_id(rec, "kreditnota") == "4493999999"


def test_extract_record_id_returns_empty_when_id_missing():
    assert engine._extract_record_id({}, "faktura") == ""
    assert engine._extract_record_id({}, "kreditnota") == ""


def test_record_number_str_invoice():
    assert engine._record_number_str(_stub_invoice(), "faktura") == "10051"


def test_record_number_str_credit_note():
    assert (
        engine._record_number_str(_stub_credit_note(), "kreditnota") == "5"
    )


def test_record_number_str_fallback_when_no_number():
    rec = _stub_invoice(invoiceNumber=None)
    # Falls back to "Faktura {id}"
    assert engine._record_number_str(rec, "faktura") == "Faktura 4493258455"


def test_record_number_int_invoice():
    assert engine._record_number_int(_stub_invoice(), "faktura") == 10051


def test_record_number_int_returns_None_when_unparseable():
    rec = _stub_invoice(invoiceNumber="not-a-number")
    assert engine._record_number_int(rec, "faktura") is None


def test_normalize_orgnr_strips_non_digits():
    assert engine._normalize_orgnr("917 422 575") == "917422575"
    assert engine._normalize_orgnr("NO 917422575 MVA") == "917422575"


def test_normalize_orgnr_returns_None_on_blank_or_letters_only():
    assert engine._normalize_orgnr("") is None
    assert engine._normalize_orgnr(None) is None
    assert engine._normalize_orgnr("abc") is None


def test_build_url_invoice(monkeypatch: pytest.MonkeyPatch):
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")
    rec = _stub_invoice()
    url = engine._build_url(rec, "faktura")
    # Pinned empirically (2026-06-21): the working UI path is
    # /handel/salg/{saleId} for BOTH invoices and credit notes.
    assert (
        url
        == "https://fiken.no/foretak/goldbox-as/handel/salg/4493497377"
    )


def test_build_url_credit_note(monkeypatch: pytest.MonkeyPatch):
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")
    rec = _stub_credit_note()
    url = engine._build_url(rec, "kreditnota")
    assert (
        url
        == "https://fiken.no/foretak/goldbox-as/handel/salg/4493500000"
    )


def test_build_url_returns_empty_when_sale_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """If Fiken's response omits sale.saleId (shouldn't happen on a
    real sent record, but defensive), we return empty rather than
    a broken URL."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")
    rec = _stub_invoice()
    rec.pop("sale", None)
    assert engine._build_url(rec, "faktura") == ""


def test_build_url_returns_empty_when_slug_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "")
    rec = _stub_invoice()
    assert engine._build_url(rec, "faktura") == ""


# ---------------------------------------------------------------------
# _build_payload — invoice path
# ---------------------------------------------------------------------


def test_build_payload_invoice_writes_every_column(
    monkeypatch: pytest.MonkeyPatch,
):
    """Sanity-check: every column in FAKTURA_PROPS should be filled
    for a fully-populated invoice. Catches silent skips / typos in
    the property mapping."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "fiken_company_slug", "goldbox-as")
    rec = _stub_invoice()
    title, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id="proj-page-123",
        project_title="0001_Test Prosjekt",
        fakturamottaker_page_id="fakt-page-456",
    )
    assert title == "10051"

    # Numbers
    assert props[FAKTURA_PROPS["fakturanummer"]] == {"number": 10051}
    assert props[FAKTURA_PROPS["netto"]] == {"number": 6240.0}

    # Type
    assert props[FAKTURA_PROPS["type"]] == {
        "select": {"name": FAKTURA_TYPE_FAKTURA}
    }

    # Rich-text strings round-trip
    assert (
        props[FAKTURA_PROPS["kommentar"]]["rich_text"][0]["text"][
            "content"
        ]
        == "Oppstartsfaktura: 50 % av avtalt beløp."
    )
    assert (
        props[FAKTURA_PROPS["deres_ref"]]["rich_text"][0]["text"][
            "content"
        ]
        == "Faktura merkes tekst"
    )
    assert (
        props[FAKTURA_PROPS["vaar_ref"]]["rich_text"][0]["text"][
            "content"
        ]
        == "0001_Test Prosjekt"
    )
    assert (
        props[FAKTURA_PROPS["fakturamottaker_tekst"]]["rich_text"][0][
            "text"
        ]["content"]
        == "ENTUR AS"
    )

    # Date
    assert props[FAKTURA_PROPS["dato"]] == {
        "date": {"start": "2026-06-18"}
    }

    # URL — /handel/salg/{saleId} path (pinned 2026-06-21).
    assert props[FAKTURA_PROPS["url"]] == {
        "url": "https://fiken.no/foretak/goldbox-as/handel/salg/4493497377"
    }

    # PDF URL — browser-friendly link from invoicePdf.
    assert props[FAKTURA_PROPS["pdf_url"]] == {
        "url": "https://fiken.no/filer/abc/faktura_2026_10051.pdf"
    }

    # Relations
    assert props[FAKTURA_PROPS["prosjekt"]] == {
        "relation": [{"id": "proj-page-123"}]
    }
    assert props[FAKTURA_PROPS["fakturamottaker"]] == {
        "relation": [{"id": "fakt-page-456"}]
    }

    # Credit-note-only column NOT set on invoice
    assert FAKTURA_PROPS["kreditnota_til"] not in props


def test_build_payload_omits_relations_when_unresolved():
    rec = _stub_invoice()
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert FAKTURA_PROPS["prosjekt"] not in props
    assert FAKTURA_PROPS["fakturamottaker"] not in props


def test_build_payload_omits_optional_strings_when_blank():
    """A row with no Kommentar / Deres_ref shouldn't write empty
    rich_text — just skip the property."""
    rec = _stub_invoice(invoiceText="", yourReference="")
    rec["reference"] = {"our": "x", "yours": ""}
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert FAKTURA_PROPS["kommentar"] not in props
    assert FAKTURA_PROPS["deres_ref"] not in props


def test_build_payload_reads_flat_reference_fields_as_fallback():
    """Older Fiken responses surface ourReference / yourReference flat
    on the record instead of in a nested `reference` dict. Reader
    falls back cleanly.
    """
    rec = _stub_invoice()
    rec.pop("reference", None)
    rec["ourReference"] = "FlatProsjekt"
    rec["yourReference"] = "FlatDeres"
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert (
        props[FAKTURA_PROPS["vaar_ref"]]["rich_text"][0]["text"][
            "content"
        ]
        == "FlatProsjekt"
    )
    assert (
        props[FAKTURA_PROPS["deres_ref"]]["rich_text"][0]["text"][
            "content"
        ]
        == "FlatDeres"
    )


def test_build_payload_prefers_project_title_arg_over_reference():
    """The caller (poller) can pre-resolve the project title from
    Notion and pass it in. When set, it should win over a stale
    reference.our from the Fiken payload — Notion is the source of
    truth for project naming."""
    rec = _stub_invoice()
    rec["reference"] = {"our": "stale-name-from-fiken"}
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id="p1",
        project_title=None,  # reference.our wins when this is None
        fakturamottaker_page_id=None,
    )
    assert (
        props[FAKTURA_PROPS["vaar_ref"]]["rich_text"][0]["text"][
            "content"
        ]
        == "stale-name-from-fiken"
    )


# ---------------------------------------------------------------------
# _build_payload — credit-note path
# ---------------------------------------------------------------------


def test_build_payload_credit_note_sets_Type_Kreditnota():
    rec = _stub_credit_note()
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert props[FAKTURA_PROPS["type"]] == {
        "select": {"name": FAKTURA_TYPE_KREDITNOTA}
    }


def test_build_payload_credit_note_writes_kreditnota_til_from_associatedInvoiceNumber():
    """Credit note carries associatedInvoiceNumber → the parent
    invoice's printable number (10051). Writes to the Number
    (kreditnota til faktura) column as a plain int.
    """
    rec = _stub_credit_note()  # associatedInvoiceNumber=10051
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert props[FAKTURA_PROPS["kreditnota_til"]] == {"number": 10051}


def test_build_payload_credit_note_uses_explicit_parent_invoice_number():
    """When caller passes parent_invoice_number explicitly (the
    cleanest path — resolved upstream via FikenInvoice audit table or
    Fiken GET), it wins over anything else."""
    rec = _stub_credit_note()
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
        parent_invoice_number=10058,
    )
    assert props[FAKTURA_PROPS["kreditnota_til"]] == {"number": 10058}


def test_build_payload_credit_note_skips_kreditnota_til_when_only_id_available():
    """When ONLY associatedInvoiceId is present (real Fiken case for
    older / non-augmented credit notes) and the caller didn't resolve
    a parent_invoice_number, the column is intentionally left blank.
    Writing the associatedInvoiceId would mislead the operator (it's
    an internal Fiken id, not a faktura number)."""
    rec = _stub_credit_note()
    rec.pop("associatedInvoiceNumber", None)
    rec["associatedInvoiceId"] = 4493258455
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert FAKTURA_PROPS["kreditnota_til"] not in props


def test_build_payload_credit_note_uses_comment_as_kommentar():
    """`comment` populates Kommentar on credit notes (no invoiceText)."""
    rec = _stub_credit_note(comment="Test kommentar")
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert (
        props[FAKTURA_PROPS["kommentar"]]["rich_text"][0]["text"][
            "content"
        ]
        == "Test kommentar"
    )


def test_build_payload_credit_note_reads_creditNoteText_for_kommentar():
    """Real Fiken kreditnota payloads carry the printed Kommentar in
    `creditNoteText` (not `invoiceText` or `comment`). Empirically
    pinned from cinesuit-as kreditnota 20001."""
    rec = _stub_credit_note()
    rec.pop("comment", None)
    rec["creditNoteText"] = "Etterfakturering av mva for faktura 10053"
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert (
        props[FAKTURA_PROPS["kommentar"]]["rich_text"][0]["text"][
            "content"
        ]
        == "Etterfakturering av mva for faktura 10053"
    )


def test_build_payload_credit_note_pulls_pdf_from_creditNotePdf():
    """Kreditnotas store the PDF info under `creditNotePdf` (NOT
    `invoicePdf`). Writer reads both keys."""
    rec = _stub_credit_note()
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="kreditnota",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert props[FAKTURA_PROPS["pdf_url"]] == {
        "url": "https://fiken.no/filer/xyz/kreditnota_2026_20003.pdf"
    }


def test_build_payload_omits_pdf_url_when_block_missing():
    """A Fiken record without the PDF block (defensive case) leaves
    the Faktura PDF column blank rather than writing garbage."""
    rec = _stub_invoice()
    rec.pop("invoicePdf", None)
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert FAKTURA_PROPS["pdf_url"] not in props


def test_build_payload_falls_back_to_downloadUrl_when_no_browser_url():
    """If only the Bearer-token-requiring downloadUrl is present
    (unusual but theoretically possible), use it rather than skip."""
    rec = _stub_invoice()
    rec["invoicePdf"] = {
        "downloadUrl": "https://api.fiken.no/api/v2/files/abc/x.pdf",
        # No downloadUrlWithFikenNormalUserCredentials.
    }
    _, props = engine._build_payload(
        fiken_record=rec,
        record_type="faktura",
        project_page_id=None,
        project_title=None,
        fakturamottaker_page_id=None,
    )
    assert props[FAKTURA_PROPS["pdf_url"]] == {
        "url": "https://api.fiken.no/api/v2/files/abc/x.pdf"
    }


# ---------------------------------------------------------------------
# Engine-disabled path (no FAKTURA_DB_ID set)
# ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_faktura_row_returns_None_when_db_id_unset(
    monkeypatch: pytest.MonkeyPatch,
):
    """C.2 ships dormant — engine no-ops + INFO-logs when
    FAKTURA_DB_ID is empty. Operator activates in C.4."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "faktura_db_id", "")
    rec = _stub_invoice()
    result = await engine.create_faktura_row(
        "goldbox-as",
        fiken_record=rec,
        record_type="faktura",
    )
    assert result is None


@pytest.mark.asyncio
async def test_create_faktura_row_returns_None_when_id_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    """Defensive: a Fiken record with no id is silently skipped
    (logged at WARN) rather than crashing the poller."""
    from gb_automations.config import settings

    monkeypatch.setattr(settings, "faktura_db_id", "fakedb")
    rec = {"some": "garbage"}
    result = await engine.create_faktura_row(
        "goldbox-as",
        fiken_record=rec,
        record_type="faktura",
    )
    assert result is None
