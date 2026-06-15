"""Unit tests for the pure helpers in sync.sync_fiken_invoice.

Pins three pieces of load-bearing logic:

  1. `_eligible_rows` — the discipline + checkbox + remainder filter
     decides which Oppgaver get billed on this run.
  2. `_build_line_items` — the (discipline, Pris) grouping + fraction
     math determines what shows up on the Fiken invoice (and the
     audit trail in FikenInvoiceLine).
  3. `_normalize_orgnr` — digits-only normalization for the org number
     read out of the Fakturamottaker DB; Fiken auto-resolves the
     customer on the org number so this is the only client-side
     transform we do.

Mocks the Notion property layout (multi_select Type + checkbox + number)
to mirror what `oppgaver_for_project` returns. Doesn't touch Postgres
or hit any HTTP API — the engine entrypoint `create_fiken_invoice`
is exercised end-to-end in the verification plan via /debug/fiken/create-draft.
"""

from __future__ import annotations

from typing import Any

import pytest

from gb_automations.config import OPPGAVER_PROPS
from gb_automations.sync import sync_fiken_invoice as engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _oppgave(
    *,
    page_id: str,
    name: str | None = None,
    discipline: str = "Interiør",
    price: float | None = 5000.0,
    skal_oppstart: bool = False,
    skal_slutt: bool = False,
    fakturert: list[str] | None = None,
    discipline_shape: str = "multi_select",
) -> dict[str, Any]:
    """Build a Notion page dict mirroring what oppgaver_for_project returns.

    Defaults to multi_select for `Type` because that's Goldbox's
    production shape (one discipline label per row). discipline_shape
    can override for the few rows that use plain `select`.

    `fakturert` is the list of labels already on the row's `Fakturert`
    multi-select (e.g. ["Oppstartsfakturert"]). Defaults to []
    (untouched row).
    """
    if discipline_shape == "multi_select":
        type_prop = {
            "type": "multi_select",
            "multi_select": [{"name": discipline}] if discipline else [],
        }
    else:
        type_prop = {
            "type": "select",
            "select": {"name": discipline} if discipline else None,
        }
    labels = fakturert or []
    title_text = name if name is not None else page_id
    return {
        "id": page_id,
        "properties": {
            "Navn": {
                "type": "title",
                "title": [{"plain_text": title_text}] if title_text else [],
            },
            OPPGAVER_PROPS["discipline"]: type_prop,
            OPPGAVER_PROPS["price_per_row"]: {
                "type": "number",
                "number": price,
            },
            OPPGAVER_PROPS["should_invoice_at_start"]: {
                "type": "checkbox",
                "checkbox": skal_oppstart,
            },
            OPPGAVER_PROPS["should_invoice_at_end"]: {
                "type": "checkbox",
                "checkbox": skal_slutt,
            },
            OPPGAVER_PROPS["billed_status"]: {
                "type": "multi_select",
                "multi_select": [{"name": name} for name in labels],
            },
        },
    }


# ---------------------------------------------------------------------------
# _eligible_rows — discipline + checkbox + remainder filter
# ---------------------------------------------------------------------------


def test_eligible_rows_oppstart_picks_only_ticked_disciplines():
    rows = [
        _oppgave(page_id="a", skal_oppstart=True),  # ✓ Interiør, ticked
        _oppgave(page_id="b", skal_oppstart=False),  # ✗ not ticked
        _oppgave(page_id="c", discipline="Klargjøre modell", skal_oppstart=True),  # ✗ not a discipline
        _oppgave(page_id="d", discipline="Eksteriør", skal_oppstart=True),  # ✓
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    ids = [row["id"] for row, _ in eligible]
    assert ids == ["a", "d"]
    # Both rows have nothing billed yet → full 1.0 remainder available.
    assert [frac for _, frac in eligible] == [1.0, 1.0]


def test_eligible_rows_oppstart_skips_already_oppstart_billed():
    rows = [
        # ✗ already oppstart-billed
        _oppgave(page_id="a", skal_oppstart=True, fakturert=["Oppstartsfakturert"]),
        # ✓ fresh — full 1.0 room
        _oppgave(page_id="b", skal_oppstart=True, fakturert=[]),
        # ✗ already slutt-billed (any slutt label kills oppstart too)
        _oppgave(page_id="c", skal_oppstart=True, fakturert=["Sluttfakturert (full)"]),
        # ✗ kreditert
        _oppgave(page_id="d", skal_oppstart=True, fakturert=["Kreditert"]),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert [row["id"] for row, _ in eligible] == ["b"]
    assert [frac for _, frac in eligible] == [1.0]


def test_eligible_rows_slutt_takes_remainder_after_oppstart():
    rows = [
        # ✓ 0.5 remainder (oppstart already done)
        _oppgave(page_id="a", skal_slutt=True, fakturert=["Oppstartsfakturert"]),
        # ✓ 1.0 remainder (fresh row)
        _oppgave(page_id="b", skal_slutt=True, fakturert=[]),
        # ✗ already slutt-billed (split)
        _oppgave(page_id="c", skal_slutt=True,
                 fakturert=["Oppstartsfakturert", "Sluttfakturert"]),
        # ✗ slutt checkbox not ticked
        _oppgave(page_id="d", skal_slutt=False, fakturert=[]),
        # ✗ slutt-full already billed (single-shot)
        _oppgave(page_id="e", skal_slutt=True, fakturert=["Sluttfakturert (full)"]),
        # ✗ kreditert
        _oppgave(page_id="f", skal_slutt=True, fakturert=["Kreditert"]),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    assert [row["id"] for row, _ in eligible] == ["a", "b"]
    assert [frac for _, frac in eligible] == [0.5, 1.0]


def test_eligible_rows_oppstart_bills_all_when_nothing_ticked():
    """Empty-picker fallback: if NO row has Skal oppstartsfaktureres
    ticked, the engine bills every eligible row. Lets the operator
    click 'oppstart' on a project with nothing pre-selected and have
    it bill everything.
    """
    rows = [
        _oppgave(page_id="a", skal_oppstart=False, fakturert=[]),
        _oppgave(page_id="b", skal_oppstart=False, fakturert=[]),
        # ✗ already oppstart-billed → still skipped even in bill-all
        _oppgave(page_id="c", skal_oppstart=False, fakturert=["Oppstartsfakturert"]),
        # ✗ Klargjøre modell → never a discipline
        _oppgave(page_id="d", discipline="Klargjøre modell", skal_oppstart=False),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert [row["id"] for row, _ in eligible] == ["a", "b"]
    assert [frac for _, frac in eligible] == [1.0, 1.0]


def test_eligible_rows_partial_mode_overrides_bill_all():
    """If ANY row is ticked, only the ticked rows bill — unticked
    rows are explicitly held back. The opposite of the bill-all
    fallback. Lets the operator stagger billing.
    """
    rows = [
        _oppgave(page_id="a", skal_oppstart=True, fakturert=[]),    # ticked
        _oppgave(page_id="b", skal_oppstart=False, fakturert=[]),   # held back
        _oppgave(page_id="c", skal_oppstart=False, fakturert=[]),   # held back
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert [row["id"] for row, _ in eligible] == ["a"]


def test_eligible_rows_slutt_bills_all_when_nothing_ticked():
    """Same fallback on the slutt side. A project at the end of its
    life: click 'sluttfaktura' with nothing ticked → bill the
    remainder of every eligible row.
    """
    rows = [
        # ✓ 0.5 remainder (oppstart was done)
        _oppgave(page_id="a", skal_slutt=False, fakturert=["Oppstartsfakturert"]),
        # ✓ 1.0 remainder (fresh — bill in full on slutt, single-shot)
        _oppgave(page_id="b", skal_slutt=False, fakturert=[]),
        # ✗ already slutt-billed
        _oppgave(page_id="c", skal_slutt=False,
                 fakturert=["Sluttfakturert (full)"]),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    assert [row["id"] for row, _ in eligible] == ["a", "b"]
    assert [frac for _, frac in eligible] == [0.5, 1.0]


def test_eligible_rows_korreksjonsrunde_filtered():
    """Korreksjonsrunde rows aren't a discipline — they should never
    make it past the eligibility filter, even with the checkbox ticked.
    """
    rows = [
        _oppgave(page_id="a", discipline="Korreksjonsrunde", skal_oppstart=True),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert eligible == []


def test_eligible_rows_rejects_bad_invoice_type():
    with pytest.raises(ValueError, match="invoice_type must be"):
        engine._eligible_rows([], "midt")


# ---------------------------------------------------------------------------
# _build_line_items — grouping + fraction math + price snapping
# ---------------------------------------------------------------------------


def test_build_line_items_one_line_per_oppgave():
    """Each Notion Oppgave becomes one Fiken line (no grouping). 5 rows
    at the same price still produce 5 lines, each named after its
    Oppgave so the customer sees individual deliverables.
    """
    rows = [
        _oppgave(
            page_id=f"i{i}",
            name=f"Bilde {i}",
            discipline="Interiør",
            price=5000.0,
            skal_oppstart=True,
        )
        for i in range(5)
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")

    assert len(lines) == 5
    assert {l.discipline for l in lines} == {"interior"}
    assert sorted(l.description for l in lines) == [f"Bilde {i}" for i in range(5)]
    assert all(l.quantity == pytest.approx(0.5) for l in lines)
    assert all(l.unit_price_ore == 500_000 for l in lines)


def test_build_line_items_keeps_each_row_separate_at_different_prices():
    """Different prices on same-discipline rows stay as separate lines.
    No price averaging — each row's price is honored as-is.
    """
    rows = [
        _oppgave(page_id="a", name="Stue", discipline="Interiør", price=4000.0, skal_oppstart=True),
        _oppgave(page_id="b", name="Kjøkken", discipline="Interiør", price=6000.0, skal_oppstart=True),
        _oppgave(page_id="c", name="Bad", discipline="Interiør", price=4000.0, skal_oppstart=True),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")

    assert len(lines) == 3
    by_page = {l.oppgave_page_id: l for l in lines}
    assert by_page["a"].unit_price_ore == 400_000
    assert by_page["a"].description == "Stue"
    assert by_page["b"].unit_price_ore == 600_000
    assert by_page["b"].description == "Kjøkken"
    assert by_page["c"].unit_price_ore == 400_000


def test_build_line_items_slutt_takes_remainder_after_oppstart():
    """A row that already had Oppstartsfakturert → slutt bills 0.5 (the
    remaining half). A fresh row → slutt bills 1.0. Each row is its own
    Fiken line either way.
    """
    rows = [
        _oppgave(page_id="a", name="Stue", discipline="Interiør", price=5000.0,
                 skal_slutt=True, fakturert=["Oppstartsfakturert"]),
        _oppgave(page_id="b", name="Kjøkken", discipline="Interiør", price=5000.0,
                 skal_slutt=True),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    lines = engine._build_line_items(eligible, invoice_type="slutt")

    assert len(lines) == 2
    by_page = {l.oppgave_page_id: l for l in lines}
    assert by_page["a"].quantity == pytest.approx(0.5)  # 0.5 remainder
    assert by_page["b"].quantity == pytest.approx(1.0)  # full row


def test_build_line_items_skips_rows_without_price():
    """A row with no Pris is logged + skipped. Other rows still produce
    a line each.
    """
    rows = [
        _oppgave(page_id="ok", name="Stue", price=5000.0, skal_oppstart=True),
        _oppgave(page_id="no_price", name="Kjøkken", price=None, skal_oppstart=True),
        _oppgave(page_id="zero", name="Bad", price=0.0, skal_oppstart=True),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")

    assert len(lines) == 1
    assert lines[0].oppgave_page_id == "ok"


def test_build_line_items_falls_back_to_discipline_name_when_no_title():
    """If an Oppgave has a blank title, line description falls back to
    the discipline's display name so the line still has something
    readable on the invoice.
    """
    rows = [
        _oppgave(page_id="a", name="", discipline="Animasjon", price=5000.0,
                 skal_oppstart=True),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")

    assert len(lines) == 1
    assert lines[0].description == "Animasjon"


def test_build_line_items_rounds_ore_correctly():
    """Half-øre values round HALF-AWAY-FROM-ZERO so we don't
    systematically under-bill. Exercised on slutt because slutt = 100%
    so the price calc isn't masked by the oppstart 0.5 cap.
    """
    rows = [
        _oppgave(page_id="a", discipline="Interiør", price=1234.567, skal_slutt=True),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    lines = engine._build_line_items(eligible, invoice_type="slutt")

    # 1234.567 NOK × 100 = 123456.7 øre → round → 123457
    assert lines[0].unit_price_ore == 123457


def test_build_line_items_oppstart_is_fifty_percent():
    """Oppstart is hardcoded to 50%: 3 rows → 3 lines of 0.5 each."""
    rows = [
        _oppgave(page_id=f"i{i}", name=f"Bilde {i}", discipline="Interiør",
                 price=5000.0, skal_oppstart=True)
        for i in range(3)
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")

    assert len(lines) == 3
    assert all(l.quantity == pytest.approx(0.5) for l in lines)


# ---------------------------------------------------------------------------
# _normalize_orgnr — strip non-digits before sending to Fiken
# ---------------------------------------------------------------------------


def test_normalize_orgnr_strips_separators():
    assert engine._normalize_orgnr("123 456 789") == "123456789"
    assert engine._normalize_orgnr("123-456-789") == "123456789"
    assert engine._normalize_orgnr("NO 123456789 MVA") == "123456789"


def test_normalize_orgnr_handles_empty_and_no_digits():
    assert engine._normalize_orgnr(None) == ""
    assert engine._normalize_orgnr("") == ""
    assert engine._normalize_orgnr("not-a-number") == ""
