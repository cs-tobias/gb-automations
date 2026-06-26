"""Unit tests for the pure helpers in sync.sync_fiken_invoice (v2).

Pins the load-bearing logic of the single-button, label-driven engine:

  1. `_eligible_rows` — discipline + `Fakturert status` filter decides
     which Oppgaver get billed on this run. The project's `Faktura
     status` decides oppstart vs. slutt mode at the call site.
  2. `_row_billable_nok` — the per-row amount math, including discount,
     mutable-Pris renegotiation between oppstart and slutt, and the
     "nothing left to bill" skip.
  3. `_build_line_items` — composes the per-Oppgave Fiken line; one
     line per row at the absolute NOK amount in øre.
  4. `_line_description` — "Navn — Beskrivelse" composition.
  5. `_normalize_orgnr` — digits-only normalization for Fiken customer
     lookup by org number.

Mocks the Notion property layout (multi_select Type + single_select
Fakturert status + numbers for Pris/Rabatt/Fakturert beløp) to mirror
what `oppgaver_for_project` returns. Doesn't touch Postgres or hit any
HTTP API — the engine entrypoint `create_fiken_invoice` is exercised
end-to-end via /debug/fiken/create-draft.
"""

from __future__ import annotations

from typing import Any

import pytest

from gb_automations.config import (
    FAKTURA_STATUS_TIL_AVSLUTNING,
    FAKTURA_STATUS_TIL_FAKTURERING,
    FAKTURA_STATUS_TIL_OPPSTART,
    FAKTURA_STATUS_TO_INVOICE_TYPE,
    FAKTURAMOTTAKER_PROPS,
    FAKTURERT_STATUS_50,
    FAKTURERT_STATUS_FULL,
    FAKTURERT_STATUS_IKKE,
    FAKTURERT_STATUS_UTGAR,
    OPPGAVER_DESC_PROP,
    OPPGAVER_PROPS,
    PROJECTS_FAKTURAMOTTAKER_PROP,
)
from gb_automations.sync import sync_fiken_invoice as engine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _oppgave(
    *,
    page_id: str,
    name: str | None = None,
    description: str | None = None,
    discipline: str = "Interiør",
    pris: float | None = 5000.0,
    rabatt_fraction: float | None = None,
    billed_amount_nok: float | None = None,
    fakturert_status: str | None = FAKTURERT_STATUS_IKKE,
    kategori: list[str] | None = None,
    discipline_shape: str = "multi_select",
) -> dict[str, Any]:
    """Build a Notion page dict mirroring what oppgaver_for_project returns
    for the v2 Fiken engine.

    Defaults to multi_select for `Type` because that's Goldbox's
    production shape (one discipline label per row). discipline_shape
    can override for the few rows that use plain `select`.

    `fakturert_status` is the option name on the row's `Fakturert status`
    single-select (or None to omit the property entirely — engine
    treats absent as "Ikke fakturert").
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
    title_text = name if name is not None else page_id
    properties: dict[str, Any] = {
        "Navn": {
            "type": "title",
            "title": [{"plain_text": title_text}] if title_text else [],
        },
        OPPGAVER_PROPS["discipline"]: type_prop,
        OPPGAVER_PROPS["price_per_row"]: {"type": "number", "number": pris},
        OPPGAVER_PROPS["discount_pct"]: {
            "type": "number",
            "number": rabatt_fraction,
        },
        OPPGAVER_PROPS["billed_amount"]: {
            "type": "number",
            "number": billed_amount_nok,
        },
        OPPGAVER_PROPS["billed_status"]: {
            "type": "select",
            "select": {"name": fakturert_status} if fakturert_status else None,
        },
        OPPGAVER_PROPS["kategori"]: {
            "type": "multi_select",
            "multi_select": [{"name": n} for n in (kategori or [])],
        },
    }
    if description is not None:
        properties[OPPGAVER_DESC_PROP] = {
            "type": "rich_text",
            "rich_text": [{"plain_text": description}] if description else [],
        }
    return {"id": page_id, "properties": properties}


# ---------------------------------------------------------------------------
# _eligible_rows — discipline + Fakturert status filter
# ---------------------------------------------------------------------------


def test_eligible_rows_oppstart_only_ikke_fakturert():
    """Bulletproof mode: only Utgår, Fakturert (already fully billed),
    Fakturert 50% (already oppstartet on this run's mode), and
    Korreksjonsrunde rows are skipped. Non-discipline rows like
    'Klargjøre modell' or blank Type DO land.
    """
    rows = [
        _oppgave(page_id="a"),  # Ikke fakturert → ✓
        _oppgave(page_id="b", fakturert_status=FAKTURERT_STATUS_50),  # ✗ already oppstartet
        _oppgave(page_id="c", fakturert_status=FAKTURERT_STATUS_FULL),  # ✗ already fully billed
        _oppgave(page_id="d", fakturert_status=FAKTURERT_STATUS_UTGAR),  # ✗ operator excluded
        # Bulletproof: non-discipline Type still lands (no silent drop).
        _oppgave(page_id="e", discipline="Klargjøre modell"),  # ✓
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert sorted(row["id"] for row in eligible) == ["a", "e"]


def test_eligible_rows_slutt_takes_ikke_and_50_percent():
    rows = [
        _oppgave(page_id="a"),  # Ikke fakturert → ✓
        _oppgave(page_id="b", fakturert_status=FAKTURERT_STATUS_50),  # ✓
        _oppgave(page_id="c", fakturert_status=FAKTURERT_STATUS_FULL),  # ✗
        _oppgave(page_id="d", fakturert_status=FAKTURERT_STATUS_UTGAR),  # ✗
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    assert [row["id"] for row in eligible] == ["a", "b"]


def test_eligible_rows_treats_blank_status_as_ikke_fakturert():
    """Blank Fakturert status (operator never picked one) → treated as
    'Ikke fakturert' so freshly cloned templates Just Work.
    """
    rows = [
        _oppgave(page_id="a", fakturert_status=None),
    ]
    assert [r["id"] for r in engine._eligible_rows(rows, "oppstart")] == ["a"]
    assert [r["id"] for r in engine._eligible_rows(rows, "slutt")] == ["a"]


def test_eligible_rows_skips_korreksjonsrunde():
    rows = [
        _oppgave(page_id="a", discipline="Korreksjonsrunde"),
    ]
    assert engine._eligible_rows(rows, "oppstart") == []
    assert engine._eligible_rows(rows, "slutt") == []


def test_eligible_rows_bulletproof_keeps_blank_and_unknown_types():
    """Bulletproof regression: rows with blank Type or any unknown Type
    label still land. The CEO's invariant is "only Utgår skips".
    Korreksjonsrunde is the one narrow exception (admin sub-rows).
    """
    rows = [
        _oppgave(page_id="blank", discipline=""),
        _oppgave(page_id="klargjor", discipline="Klargjøre modell"),
        _oppgave(page_id="rando", discipline="SomeNewTypeNobodyAddedYet"),
    ]
    assert sorted(r["id"] for r in engine._eligible_rows(rows, "oppstart")) == [
        "blank",
        "klargjor",
        "rando",
    ]
    assert sorted(r["id"] for r in engine._eligible_rows(rows, "slutt")) == [
        "blank",
        "klargjor",
        "rando",
    ]


def test_eligible_rows_rejects_bad_invoice_type():
    with pytest.raises(ValueError, match="invoice_type must be"):
        engine._eligible_rows([], "midt")


# ---------------------------------------------------------------------------
# Postgres-derived "row is on an unsent draft" gate
# ---------------------------------------------------------------------------


def test_eligible_rows_skips_oppgave_in_rows_on_unsent_drafts_oppstart():
    """When a row's page id is in the rows_on_unsent_drafts set (because
    its FikenInvoiceLine references an unsent FikenInvoice), the
    eligibility filter skips it — defensive belt-and-suspenders with
    the per-project block check in create_fiken_invoice."""
    rows = [
        _oppgave(page_id="a"),  # ✓
        _oppgave(page_id="b"),  # ✗ on unsent draft
    ]
    eligible = engine._eligible_rows(
        rows, "oppstart", rows_on_unsent_drafts={"b"}
    )
    assert [r["id"] for r in eligible] == ["a"]


def test_eligible_rows_skips_oppgave_in_rows_on_unsent_drafts_slutt():
    """Same gate fires on slutt — there's only one unsent draft per
    project (block check enforces it), so if a row is on one, no new
    draft mode should bill it."""
    rows = [
        _oppgave(page_id="a"),  # ✓
        _oppgave(page_id="b"),  # ✗
    ]
    eligible = engine._eligible_rows(
        rows, "slutt", rows_on_unsent_drafts={"b"}
    )
    assert [r["id"] for r in eligible] == ["a"]


def test_eligible_rows_empty_unsent_drafts_set_is_permissive():
    """Default kwarg (None) and explicit empty set both behave the same:
    no draft-gating, all rows pass the unsent-draft check."""
    rows = [_oppgave(page_id="a"), _oppgave(page_id="b")]
    assert sorted(
        r["id"] for r in engine._eligible_rows(rows, "oppstart")
    ) == ["a", "b"]
    assert sorted(
        r["id"]
        for r in engine._eligible_rows(
            rows, "oppstart", rows_on_unsent_drafts=set()
        )
    ) == ["a", "b"]


# ---------------------------------------------------------------------------
# _row_billable_nok — discount, renegotiation, "nothing left" skip
# ---------------------------------------------------------------------------


def test_row_billable_oppstart_basic_50_percent():
    """No rabatt — to_bill = 5000 (half of gross). The contract is:
    `unit_price` is always Pris (10_000), `quantity_fraction` is 0.5,
    Fiken's `Antall × Pris × (1 − Rabatt%)` math lands on 5000.
    """
    row = _oppgave(page_id="a", pris=10_000.0)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "oppstart")
    assert to_bill == 5000.0
    assert new_total == 5000.0
    assert unit_price == 10_000.0           # ALWAYS Pris
    assert quantity_fraction == 0.5
    assert discount_fraction == 0.0


def test_row_billable_oppstart_with_rabatt():
    """Rabatt 10% (Notion Percent format → 0.10) on 10000 NOK, oppstart:
        unit_price        = Pris = 10_000          (sent to Fiken)
        quantity_fraction = 0.5                    (sent to Fiken as Antall)
        discount          = 10%                    (sent to Fiken)
        to_bill           = gross × 0.5 = 4500     (Notion ledger / audit)
    Fiken's printed invoice: Antall 0,5 | Pris kr 10 000 | Rabatt 10 % |
    Sum kr 4500.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.10)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "oppstart")
    assert to_bill == 4500.0
    assert new_total == 4500.0
    assert unit_price == 10_000.0
    assert quantity_fraction == 0.5
    assert discount_fraction == 0.10


def test_row_billable_slutt_subtracts_unsent_drafted_nok():
    """Oppstart was DRAFTED but never sent in Fiken — so Fakturert
    beløp on the Notion row is still 0 (we only write that on
    graduation). But the audit table holds the drafted amount.

    Pris 10_000, oppstart drafted = 5000 unsent, slutt math should
    bill the remaining 5000, NOT the full 10_000.
    """
    row = _oppgave(page_id="a", pris=10_000.0, billed_amount_nok=0.0)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(
        row, "slutt", already_drafted_nok=5000.0
    )
    assert to_bill == 5000.0
    # new_total = already_committed (5000) + to_bill (5000) = 10_000.
    # The graduation step computes the actual Notion cumulative
    # separately (it reads current Fakturert beløp + adds the
    # audit-line NOK), so new_total here is informational only.
    assert new_total == 10_000.0
    assert unit_price == 10_000.0
    assert quantity_fraction == 0.5


def test_row_billable_slutt_skips_when_drafted_already_covers_gross():
    """Oppstart drafted for the FULL gross (e.g. operator made a
    single-shot full-bill oppstart draft for testing); slutt math
    sees remaining=0 and skips.
    """
    row = _oppgave(page_id="a", pris=10_000.0, billed_amount_nok=0.0)
    result = engine._row_billable_nok(
        row, "slutt", already_drafted_nok=10_000.0
    )
    assert result is None


def test_row_billable_oppstart_ignores_drafted_kwarg():
    """Oppstart math is unchanged by the unsent-drafted kwarg
    (defaults to 0; even if passed in, oppstart computes 50% of
    gross independent of prior drafts)."""
    row = _oppgave(page_id="a", pris=10_000.0, billed_amount_nok=0.0)
    (
        to_bill_default,
        _,
        _,
        qf_default,
        _,
    ) = engine._row_billable_nok(row, "oppstart")
    (
        to_bill_with_drafted,
        _,
        _,
        qf_with_drafted,
        _,
    ) = engine._row_billable_nok(
        row, "oppstart", already_drafted_nok=999_999.0
    )
    assert to_bill_default == to_bill_with_drafted == 5000.0
    assert qf_default == qf_with_drafted == 0.5


def test_row_billable_slutt_remainder_after_oppstart():
    """Oppstart booked 7500 (50% of original 15K). Pris renegotiated
    down to 10000. Slutt run should bill 10000 − 7500 = 2500. With
    the new math:
        gross             = 10_000
        remaining         = 2_500
        quantity_fraction = remaining / gross = 0.25
        unit_price        = Pris = 10_000      (UNCHANGED across runs)
    Fiken's line: Antall 0,25 | Pris kr 10 000 | Sum kr 2 500.
    """
    row = _oppgave(page_id="a", pris=10_000.0, billed_amount_nok=7500.0)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "slutt")
    assert to_bill == 2500.0
    assert new_total == 10_000.0
    assert unit_price == 10_000.0
    assert quantity_fraction == 0.25
    assert discount_fraction == 0.0


def test_row_billable_slutt_with_rabatt_uses_quantity_fraction():
    """Slutt, Rabatt 20%, fresh row (no prior oppstart):
        gross             = 8000  (= 10000 × 0.8)
        remaining         = 8000
        quantity_fraction = 8000 / 8000 = 1.0
        unit_price        = Pris = 10_000  (full undiscounted)
    Fiken prints: Antall 1 | Pris kr 10 000 | Rabatt 20 % | Sum kr 8000.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.20)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "slutt")
    assert to_bill == 8000.0
    assert new_total == 8000.0
    assert unit_price == 10_000.0
    assert quantity_fraction == 1.0
    assert discount_fraction == 0.20


def test_row_billable_slutt_with_rabatt_after_oppstart():
    """The composite case the v3 math is meant to handle cleanly: oppstart
    booked half of the rabatted gross, slutt bills the rest, all without
    the inverted-divide-by-(1-rabatt) hack the v2 math used.

    Pris=10_000, Rabatt=15% → gross=8500. Oppstart booked 4250 (= half).
    Slutt: remaining = 8500 − 4250 = 4250. quantity_fraction = 4250/8500
    = 0.5. unit_price stays at Pris = 10_000. Fiken's `Antall × Pris ×
    (1 − Rabatt)` = 0.5 × 10000 × 0.85 = 4250. ✓
    """
    row = _oppgave(
        page_id="a",
        pris=10_000.0,
        rabatt_fraction=0.15,
        billed_amount_nok=4250.0,
    )
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "slutt")
    assert to_bill == 4250.0
    assert new_total == 8500.0
    assert unit_price == 10_000.0
    assert quantity_fraction == 0.5
    assert discount_fraction == 0.15


def test_row_billable_slutt_skips_when_remaining_zero_or_negative():
    """Renegotiation can push remaining ≤ 0 (operator dropped Pris below
    what was already invoiced). Skip cleanly — no negative line in Fiken.
    """
    row = _oppgave(page_id="a", pris=5000.0, billed_amount_nok=7500.0)
    assert engine._row_billable_nok(row, "slutt") is None


def test_row_billable_missing_or_zero_price_returns_none():
    """Missing or zero Pris → None (not a billable line). The client does
    not want kr 0 lines on the draft; such rows are filtered by
    _eligible_rows, and _row_billable_nok returns None for coherence
    with direct callers like /debug/fiken/inspect.
    """
    for case in (
        _oppgave(page_id="a", pris=None),
        _oppgave(page_id="b", pris=0.0),
    ):
        assert engine._row_billable_nok(case, "oppstart") is None
        assert engine._row_billable_nok(case, "slutt") is None


def test_row_billable_rabatt_uses_notion_percent_format():
    """Regression for the 4992-vs-5000 bug. Operator set Rabatt to 15%
    in Notion's Percent-formatted column; Notion's API returns the
    value as the fraction 0.15 (NOT 15). Engine must treat it as a
    fraction directly. Pris=10000 + Rabatt=0.15 + oppstart →
    gross=8500 → to_bill=4250.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.15)
    (
        to_bill,
        new_total,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row, "oppstart")
    assert to_bill == 4250.0
    assert new_total == 4250.0
    assert unit_price == 10_000.0          # Pris, unchanged
    assert quantity_fraction == 0.5
    assert discount_fraction == 0.15


def test_row_billable_clamps_rabatt_to_valid_range():
    """Negative Rabatt collapses to 0 (no discount); above 1.0 (i.e.
    >100%) collapses to 1.0 and the row contributes 0. Defensive —
    Notion's number field has no built-in bounds, and an operator typing
    `150` into a Percent column gets stored as 1.5.
    """
    row_negative = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=-0.50)
    (
        to_bill,
        _,
        unit_price,
        quantity_fraction,
        discount_fraction,
    ) = engine._row_billable_nok(row_negative, "oppstart")
    assert to_bill == 5000.0  # rabatt clamped to 0 → full price
    assert unit_price == 10_000.0
    assert quantity_fraction == 0.5
    assert discount_fraction == 0.0

    # 100%+ rabatt → gross = 0 → row lands as a kr 0 line (bulletproof).
    row_huge = _oppgave(page_id="b", pris=10_000.0, rabatt_fraction=1.5)
    result = engine._row_billable_nok(row_huge, "oppstart")
    assert result is not None
    to_bill_huge, _, unit_price_huge, _, _ = result
    assert to_bill_huge == 0.0
    assert unit_price_huge == 10_000.0  # Pris unchanged; rabatt clamped to 100%


# ---------------------------------------------------------------------------
# _build_line_items — one line per Oppgave; async because of product lookup
# ---------------------------------------------------------------------------
#
# `_build_line_items` calls `_resolve_kategori_to_product_id` which hits
# Fiken on cache miss. Tests monkeypatch that resolver with a tiny stub
# so we don't list_products in unit tests. Default stub returns None
# (no product match), exercising the free-text fallback path.


def _stub_resolver(mapping: dict[str, int] | None = None):
    """Return an async function suitable for monkeypatching
    engine._resolve_kategori_to_product_id. The returned coroutine looks
    up the kategori label in `mapping` and returns the productId or None.
    """
    table = mapping or {}

    async def _stub(company_slug: str, kategori_label: str) -> int | None:
        return table.get(kategori_label)

    return _stub


@pytest.mark.asyncio
async def test_build_line_items_one_line_per_oppgave(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(page_id=f"i{i}", name=f"Bilde {i}", pris=5000.0)
        for i in range(5)
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    assert len(lines) == 5
    assert {l.discipline for l in lines} == {"interior"}
    # No Kategori on these rows → description falls back to the Navn.
    assert sorted(l.description for l in lines) == [
        f"Bilde {i}" for i in range(5)
    ]
    # Unit price is the FULL Pris (5000) in øre; quantity = 0.5 oppstart;
    # amount_nok_ore is the post-rabatt NOK (= unit × quantity here, no
    # rabatt) = 2500 NOK in øre = 250_000.
    assert all(l.unit_price_nok_ore == 500_000 for l in lines)
    assert all(l.quantity_fraction == 0.5 for l in lines)
    assert all(l.amount_nok_ore == 250_000 for l in lines)
    assert all(l.discount_percent == 0.0 for l in lines)
    # No kategori → no product link → free-text fallback.
    assert all(l.product_id is None for l in lines)


@pytest.mark.asyncio
async def test_build_line_items_oppstart_with_rabatt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Pris=10000, Rabatt=15%, oppstart:
        unit_price_nok_ore = 1_000_000      (full Pris in øre)
        quantity_fraction  = 0.5
        discount_percent   = 15.0
        amount_nok_ore     = 425_000        (= 4250 NOK post-rabatt)
    Fiken's printed line: Antall 0,5 | Pris kr 10 000 | Rabatt 15% |
    Sum kr 4250.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [_oppgave(page_id="a", name="Stue", pris=10_000.0, rabatt_fraction=0.15)]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    assert len(lines) == 1
    assert lines[0].unit_price_nok_ore == 1_000_000
    assert lines[0].quantity_fraction == 0.5
    assert lines[0].discount_percent == 15.0
    assert lines[0].amount_nok_ore == 425_000


@pytest.mark.asyncio
async def test_build_line_items_slutt_with_renegotiation(
    monkeypatch: pytest.MonkeyPatch,
):
    """billed_amount=7500 (from prior 50% of 15K Pris); Pris now 10000.
    Slutt: remaining=2500, quantity_fraction=0.25, unit_price=Pris=10K.
    Audit trail amount = 2500 NOK = 250_000 øre.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(
            page_id="a",
            name="Stue",
            pris=10_000.0,
            billed_amount_nok=7500.0,
            fakturert_status=FAKTURERT_STATUS_50,
        ),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    lines = await engine._build_line_items(
        eligible, invoice_type="slutt", company_slug="probe"
    )
    assert len(lines) == 1
    assert lines[0].unit_price_nok_ore == 1_000_000
    assert lines[0].quantity_fraction == 0.25
    assert lines[0].amount_nok_ore == 250_000
    assert lines[0].new_billed_amount_nok == 10_000.0


def test_eligible_rows_skips_missing_or_zero_price():
    """A row with no Pris (or Pris ≤ 0) is NOT a billable line — excluded
    by eligibility, exactly like a Korreksjonsrunde row. The client does
    not want kr 0 lines on the Fiken draft, and a skipped row gets no
    audit line so it's invisible to graduation matching too.
    """
    rows = [
        _oppgave(page_id="ok", name="Stue", pris=5000.0),
        _oppgave(page_id="no_price", name="Kjøkken", pris=None),
        _oppgave(page_id="zero", name="Bad", pris=0.0),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert [r["id"] for r in eligible] == ["ok"]
    # Same on slutt.
    eligible_slutt = engine._eligible_rows(rows, "slutt")
    assert [r["id"] for r in eligible_slutt] == ["ok"]


@pytest.mark.asyncio
async def test_build_line_items_only_priced_rows_land(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end: after eligibility filters zero-price rows, only the
    priced row produces a Fiken line.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(page_id="ok", name="Stue", pris=5000.0),
        _oppgave(page_id="no_price", name="Kjøkken", pris=None),
        _oppgave(page_id="zero", name="Bad", pris=0.0),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    by_id = {l.oppgave_page_id: l for l in lines}
    assert set(by_id) == {"ok"}
    assert by_id["ok"].amount_nok_ore == 250_000  # 50% of 5000
    assert by_id["ok"].unit_price_nok_ore == 500_000


@pytest.mark.asyncio
async def test_build_line_items_skips_zero_remainder_on_slutt(
    monkeypatch: pytest.MonkeyPatch,
):
    """Renegotiation leaves nothing to bill → no line for that row."""
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(
            page_id="a",
            pris=5000.0,
            billed_amount_nok=7500.0,
            fakturert_status=FAKTURERT_STATUS_50,
        ),
        _oppgave(page_id="b", pris=5000.0, fakturert_status=FAKTURERT_STATUS_IKKE),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    lines = await engine._build_line_items(
        eligible, invoice_type="slutt", company_slug="probe"
    )
    assert [l.oppgave_page_id for l in lines] == ["b"]
    # Full 5000 NOK billed → unit_price 5000 × 100, quantity 1.0,
    # amount_nok_ore 500_000.
    assert lines[0].unit_price_nok_ore == 500_000
    assert lines[0].quantity_fraction == 1.0
    assert lines[0].amount_nok_ore == 500_000


# ---------------------------------------------------------------------------
# _line_product_name + _line_comment — Kategori-driven Fiken line text
# ---------------------------------------------------------------------------
#
# product_name = Fiken `description` (bold first line on the invoice)
#              = Oppgave Kategori label, OR the Navn — Beskrivelse fallback
# comment      = Fiken `comment` (smaller sub-line)
#              = "Navn - Beskrivelse" (or just Navn when Beskrivelse blank)


def test_line_product_name_uses_kategori_when_set():
    row = _oppgave(
        page_id="a",
        name="Kjøkken",
        description="Hovedbilde dag",
        kategori=["Næring - Interiør"],
    )
    assert engine._line_product_name(row, "Næring - Interiør") == "Næring - Interiør"


def test_line_product_name_falls_back_to_navn_beskrivelse_when_no_kategori():
    row = _oppgave(page_id="a", name="Kjøkken", description="Hovedbilde dag")
    assert engine._line_product_name(row, None) == "Kjøkken — Hovedbilde dag"


def test_line_product_name_falls_back_to_navn_alone_when_beskrivelse_blank():
    row = _oppgave(page_id="a", name="Kjøkken", description="")
    assert engine._line_product_name(row, None) == "Kjøkken"
    row_no_desc = _oppgave(page_id="b", name="Kjøkken")
    assert engine._line_product_name(row_no_desc, None) == "Kjøkken"


def test_line_product_name_falls_back_to_discipline_when_all_blank():
    row = _oppgave(page_id="a", name="", description=None, discipline="Animasjon")
    assert engine._line_product_name(row, None) == "Animasjon"


def test_line_comment_navn_dash_beskrivelse():
    row = _oppgave(page_id="a", name="Kjøkken", description="Hovedbilde dag")
    # Single dash (not em-dash) and a hyphenated separator so the Fiken
    # invoice's sub-line reads cleanly under the bold kategori line.
    assert engine._line_comment(row) == "Kjøkken - Hovedbilde dag"


def test_line_comment_navn_alone_when_beskrivelse_blank():
    row = _oppgave(page_id="a", name="Kjøkken", description="")
    assert engine._line_comment(row) == "Kjøkken"
    row_no_desc = _oppgave(page_id="b", name="Kjøkken")
    assert engine._line_comment(row_no_desc) == "Kjøkken"


def test_line_comment_empty_when_all_blank():
    row = _oppgave(page_id="a", name="", description=None)
    assert engine._line_comment(row) == ""


# ---------------------------------------------------------------------------
# _resolve_kategori_label + product resolution (Kategori → Fiken product)
# ---------------------------------------------------------------------------


def test_resolve_kategori_label_picks_first_when_multiple():
    """Multi-select can carry several labels — engine picks the first
    (operator's selection order is preserved by Notion).
    """
    row = _oppgave(page_id="a", kategori=["Næring - Interiør", "Print"])
    assert engine._resolve_kategori_label(row) == "Næring - Interiør"


def test_resolve_kategori_label_blank_returns_none():
    row = _oppgave(page_id="a")  # kategori defaults to []
    assert engine._resolve_kategori_label(row) is None


@pytest.mark.asyncio
async def test_resolve_kategori_to_product_id_via_stubbed_resolver(
    monkeypatch: pytest.MonkeyPatch,
):
    """End-to-end through _build_line_items with a stubbed resolver:
    Kategori 'Næring - Interiør' matches Fiken product id 4493261141.
    Resulting line carries product_id=4493261141 AND the Kategori label
    as description.
    """
    monkeypatch.setattr(
        engine,
        "_resolve_kategori_to_product_id",
        _stub_resolver({"Næring - Interiør": 4493261141}),
    )
    rows = [
        _oppgave(
            page_id="a",
            name="Kjøkken",
            description="Hovedbilde dag",
            pris=10_000.0,
            kategori=["Næring - Interiør"],
        )
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    assert len(lines) == 1
    line = lines[0]
    assert line.product_id == 4493261141
    assert line.description == "Næring - Interiør"
    assert line.comment == "Kjøkken - Hovedbilde dag"
    # Pris stays the same in øre; quantity_fraction = 0.5 for oppstart.
    assert line.unit_price_nok_ore == 1_000_000
    assert line.quantity_fraction == 0.5
    assert line.amount_nok_ore == 500_000


@pytest.mark.asyncio
async def test_build_line_items_kategori_with_no_product_falls_back_to_free_text(
    monkeypatch: pytest.MonkeyPatch,
):
    """When the Kategori label doesn't match any Fiken product, the line
    is built as free-text: product_id=None, description=Kategori label.
    Fiken will reject this with a 400 ("incomeAccount is required for
    free-text lines"), which is the operator's prompt to add the missing
    product in Fiken — but the line dataclass itself is well-formed and
    survives engine-side post-build assertions.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(
            page_id="a",
            name="Plakat A1",
            description="20 stk",
            pris=4000.0,
            kategori=["Bolig - Foo (unmapped)"],
        )
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    assert len(lines) == 1
    assert lines[0].product_id is None
    assert lines[0].description == "Bolig - Foo (unmapped)"
    assert lines[0].comment == "Plakat A1 - 20 stk"


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


# ---------------------------------------------------------------------------
# _resolve_project_orgnr — graceful behavior when Notion is missing data
# ---------------------------------------------------------------------------
#
# These pin the "missing Orgnr is no longer an error" path. Engine returns
# (None, _) instead of raising; the caller in create_fiken_invoice then
# sends the Fiken draft without a customer link and the operator picks/
# creates one in Fiken's UI before clicking Send.


def _project_page_with_fakturamottaker(fakt_ids: list[str]) -> dict[str, Any]:
    """Build a minimal Project page dict carrying a Fakturamottaker relation
    with the given page ids (or empty for the "no Fakturamottaker linked"
    case). Other props omitted — `_resolve_project_orgnr` only reads this
    one relation off the project.
    """
    return {
        "id": "project-1",
        "properties": {
            PROJECTS_FAKTURAMOTTAKER_PROP: {
                "type": "relation",
                "relation": [{"id": pid} for pid in fakt_ids],
            },
        },
    }


def _fakturamottaker_page(
    *, page_id: str, title: str | None, orgnr: str | None
) -> dict[str, Any]:
    """Build a minimal Fakturamottaker page dict — title + Orgnr rich_text.
    Pass orgnr=None to simulate the "row exists but Orgnr column blank"
    case the user reported.
    """
    return {
        "id": page_id,
        "properties": {
            "Navn": {
                "type": "title",
                "title": [{"plain_text": title}] if title else [],
            },
            FAKTURAMOTTAKER_PROPS["orgnr"]: {
                "type": "rich_text",
                "rich_text": [{"plain_text": orgnr}] if orgnr else [],
            },
        },
    }


@pytest.mark.asyncio
async def test_resolve_project_orgnr_returns_none_when_no_fakturamottaker_relation():
    """No Fakturamottaker linked on the project at all → (None, None).
    The caller treats this as "create the draft without a customer."
    """
    project_page = _project_page_with_fakturamottaker([])
    orgnr, name = await engine._resolve_project_orgnr(project_page)
    assert orgnr is None
    assert name is None


@pytest.mark.asyncio
async def test_resolve_project_orgnr_returns_name_when_fakturamottaker_set_but_orgnr_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fakturamottaker linked but its Orgnr column is blank → (None, name).
    The name is still useful — the engine's log line names which
    Fakturamottaker row needs an Orgnr later.
    """
    project_page = _project_page_with_fakturamottaker(["fakt-1"])
    fakt_page = _fakturamottaker_page(
        page_id="fakt-1", title="Sjøgata Holding", orgnr=None
    )

    async def fake_get_page(page_id: str) -> dict[str, Any]:
        assert page_id == "fakt-1"
        return fakt_page

    monkeypatch.setattr(
        engine.notion_client, "get_page", fake_get_page
    )
    orgnr, name = await engine._resolve_project_orgnr(project_page)
    assert orgnr is None
    assert name == "Sjøgata Holding"


# ---------------------------------------------------------------------------
# read_select_name — fallback between Notion's `select` and `status` shapes
# ---------------------------------------------------------------------------
#
# The CEO models the workspace's one-of-many columns (Faktura status,
# Fakturert status, lifecycle Status) as Notion's `status` property type,
# not `select`. They're functionally identical from the engine's
# perspective (one option name per row), but the API payload uses a
# different key. The reader falls back so a column flipped from select
# to status keeps working without code changes.


def test_read_select_name_handles_status_property_shape():
    """Notion's `status` property type uses {"status": {"name": "X"}}
    instead of {"select": {"name": "X"}} — same payload shape, different
    wrapper. The reader returns the option name regardless.
    """
    page = {
        "properties": {
            "Faktura status": {
                "type": "status",
                "status": {"name": "Til oppstartsfaktura"},
            },
        },
    }
    assert (
        engine.notion_client.read_select_name(page, "Faktura status")
        == "Til oppstartsfaktura"
    )


def test_read_select_name_still_handles_legacy_select_shape():
    """Same column modeled as a Select still reads cleanly. Lets the
    operator flip the column between types without breaking the engine.
    """
    page = {
        "properties": {
            "Faktura status": {
                "type": "select",
                "select": {"name": "Til avslutningsfaktura"},
            },
        },
    }
    assert (
        engine.notion_client.read_select_name(page, "Faktura status")
        == "Til avslutningsfaktura"
    )


# ---------------------------------------------------------------------------
# FAKTURA_STATUS_TO_INVOICE_TYPE — operator-facing synonyms map to one engine
# ---------------------------------------------------------------------------
#
# The CEO writes "Til fakturering" in Notion; the original brief used
# "Til avslutningsfaktura". Both must resolve to the same engine mode
# (slutt) so the team can pick whichever reads better.


def test_faktura_status_synonyms_both_map_to_slutt():
    assert FAKTURA_STATUS_TO_INVOICE_TYPE[FAKTURA_STATUS_TIL_OPPSTART] == "oppstart"
    assert FAKTURA_STATUS_TO_INVOICE_TYPE[FAKTURA_STATUS_TIL_AVSLUTNING] == "slutt"
    assert FAKTURA_STATUS_TO_INVOICE_TYPE[FAKTURA_STATUS_TIL_FAKTURERING] == "slutt"


# ---------------------------------------------------------------------------
# _ensure_placeholder_contact — exists with the expected single-arg shape
# ---------------------------------------------------------------------------
#
# The full behavior (Fiken create + DB upsert + cache hit/miss) is verified
# end-to-end against a personal Fiken account. This test catches the
# rename/refactor regression where someone removes the function or changes
# its arity, which would break the engine's no-Orgnr branch silently.


def test_ensure_placeholder_contact_exists_and_takes_company_slug():
    import inspect

    assert hasattr(engine, "_ensure_placeholder_contact")
    sig = inspect.signature(engine._ensure_placeholder_contact)
    assert list(sig.parameters) == ["company_slug"]


# ---------------------------------------------------------------------------
# Draft-level "Kommentar" (invoiceText) — picked per invoice_type from env
# ---------------------------------------------------------------------------
#
# Pure-config pinning — the mode-picking logic in create_fiken_invoice is
# trivial (a ternary) but the field NAMES on settings are env-driven, and
# code that references the wrong name silently regresses to no message.
# These tests catch a rename of either setting.


def test_invoice_text_settings_exist_for_both_modes():
    from gb_automations.config import settings

    assert hasattr(settings, "fiken_invoice_text_oppstart")
    assert hasattr(settings, "fiken_invoice_text_slutt")
    # Defaults are populated; engine uses them when .env doesn't override.
    assert settings.fiken_invoice_text_oppstart.strip()
    assert settings.fiken_invoice_text_slutt.strip()


def test_invoice_text_oppstart_and_slutt_are_distinct():
    """The two texts must NOT be the same default — that would mean the
    operator's distinction between an oppstart and a slutt invoice gets
    lost on the printed message. Independent defaults force the operator
    to consciously choose each one.
    """
    from gb_automations.config import settings

    assert (
        settings.fiken_invoice_text_oppstart
        != settings.fiken_invoice_text_slutt
    )


# ---------------------------------------------------------------------------
# _ensure_bank_account_number — shape check; full behavior via end-to-end
# ---------------------------------------------------------------------------


def test_ensure_bank_account_number_exists_and_takes_company_slug():
    import inspect

    assert hasattr(engine, "_ensure_bank_account_number")
    sig = inspect.signature(engine._ensure_bank_account_number)
    assert list(sig.parameters) == ["company_slug"]


def test_fiken_bank_account_number_setting_defaults_to_empty():
    """Env override is opt-in. Empty default → engine auto-picks the
    first active normal-type account from Fiken on first call.
    """
    from gb_automations.config import settings

    assert hasattr(settings, "fiken_bank_account_number")
    assert settings.fiken_bank_account_number == ""


# ---------------------------------------------------------------------------
# Brreg — pick_exact_match (pure helper; the load-bearing part of the new path)
# ---------------------------------------------------------------------------


def test_pick_exact_match_picks_query_plus_AS():
    from gb_automations.clients import brreg

    results = [
        {"navn": "ENTUR AS", "organisasjonsnummer": "917422575"},
        {"navn": "ENTUR LANDSFORENING", "organisasjonsnummer": "985515832"},
        {"navn": "ENTURA HOLDING AS", "organisasjonsnummer": "914984106"},
        {"navn": "ENTURA EIENDOMSDRIFT AS", "organisasjonsnummer": "992816295"},
    ]
    chosen = brreg.pick_exact_match("Entur", results)
    assert chosen is not None
    assert chosen["organisasjonsnummer"] == "917422575"


def test_pick_exact_match_exact_equals_wins():
    from gb_automations.clients import brreg

    # Operator typed the full legal name including suffix → exact equals.
    results = [{"navn": "EQUINOR ASA", "organisasjonsnummer": "923609016"}]
    chosen = brreg.pick_exact_match("Equinor ASA", results)
    assert chosen is not None
    assert chosen["organisasjonsnummer"] == "923609016"


def test_pick_exact_match_case_insensitive():
    from gb_automations.clients import brreg

    results = [{"navn": "ENTUR AS", "organisasjonsnummer": "917422575"}]
    assert brreg.pick_exact_match("entur", results) is not None
    assert brreg.pick_exact_match("ENTUR", results) is not None
    assert brreg.pick_exact_match("  Entur  ", results) is not None


def test_pick_exact_match_returns_none_when_no_clean_match():
    """When no result is `Entur` or `Entur <suffix>`, return None — never
    guess. Avoids creating the wrong customer for ambiguous searches.
    """
    from gb_automations.clients import brreg

    results = [
        {"navn": "ENTURA HOLDING AS", "organisasjonsnummer": "914984106"},
        {"navn": "ENTURA EIENDOMSDRIFT AS", "organisasjonsnummer": "992816295"},
    ]
    assert brreg.pick_exact_match("Entur", results) is None


def test_pick_exact_match_returns_none_on_multiple_clean_matches():
    """Two results both match cleanly (e.g. 'Foo' has both 'FOO AS' and
    'FOO ASA') → return None. We refuse to pick between them.
    """
    from gb_automations.clients import brreg

    results = [
        {"navn": "FOO AS", "organisasjonsnummer": "111111111"},
        {"navn": "FOO ASA", "organisasjonsnummer": "222222222"},
    ]
    assert brreg.pick_exact_match("Foo", results) is None


def test_pick_exact_match_returns_none_on_empty_inputs():
    from gb_automations.clients import brreg

    assert brreg.pick_exact_match("", []) is None
    assert brreg.pick_exact_match("Entur", []) is None
    assert (
        brreg.pick_exact_match(
            "", [{"navn": "ENTUR AS", "organisasjonsnummer": "1"}]
        )
        is None
    )


def test_pick_exact_match_covers_multiple_suffixes():
    """Sanity check: a few uncommon suffixes from NORWEGIAN_ENTITY_SUFFIXES
    also match. Keeps the suffix list honest if someone tightens it later.
    """
    from gb_automations.clients import brreg

    cases = [
        ("Statkraft", "STATKRAFT SF"),  # SF not in suffix list → should NOT match
        ("Norsk Hydro", "NORSK HYDRO ASA"),
        ("Foo", "FOO ANS"),
        ("Bar", "BAR DA"),
        ("Baz", "BAZ ENK"),
        ("Quux", "QUUX NUF"),
    ]
    for query, name in cases:
        results = [{"navn": name, "organisasjonsnummer": "X"}]
        match = brreg.pick_exact_match(query, results)
        if name.split()[-1] in brreg.NORWEGIAN_ENTITY_SUFFIXES:
            assert match is not None, f"expected {name!r} to match for {query!r}"
        else:
            assert match is None, f"{name!r} should NOT match for {query!r}"


# ---------------------------------------------------------------------------
# Brreg engine helpers — shape checks
# ---------------------------------------------------------------------------


def test_brreg_helpers_exist_in_engine():
    import inspect

    assert hasattr(engine, "_brreg_enrich_name")
    assert list(inspect.signature(engine._brreg_enrich_name).parameters) == [
        "orgnr",
        "fallback_name",
    ]

    assert hasattr(engine, "_resolve_project_via_kunder_brreg")
    assert list(
        inspect.signature(engine._resolve_project_via_kunder_brreg).parameters
    ) == ["project_page"]

    assert hasattr(engine, "_update_fakturamottaker_in_notion")
    assert list(
        inspect.signature(engine._update_fakturamottaker_in_notion).parameters
    ) == ["project_page", "orgnr", "brreg_name"]


def test_notion_client_writeback_helpers_exist():
    """The engine's Brreg writeback path leans on three new Notion
    helpers. Shape check catches a future accidental rename.
    """
    import inspect

    from gb_automations.clients import notion as n

    assert hasattr(n, "create_page_in_db")
    params = inspect.signature(n.create_page_in_db).parameters
    assert "db_id" in params
    assert "title" in params

    assert hasattr(n, "set_page_title")
    assert "page_id" in inspect.signature(n.set_page_title).parameters

    assert hasattr(n, "set_relation")
    params = inspect.signature(n.set_relation).parameters
    assert "prop_name" in params
    assert "target_ids" in params


def test_fakturamottaker_props_includes_navn_for_writeback():
    """The Brreg writeback needs to know which column is the title on
    the Fakturamottaker DB so it can update it. Pinning the constant
    here catches an accidental rename.
    """
    from gb_automations.config import FAKTURAMOTTAKER_PROPS

    assert FAKTURAMOTTAKER_PROPS.get("navn") == "Navn"
    assert FAKTURAMOTTAKER_PROPS.get("orgnr") == "Orgnr"


# ---------------------------------------------------------------------------
# Bulletproof regressions — pin the CEO's invariant
# ---------------------------------------------------------------------------
#
# "Only Utgår skips" (plus the narrow Korreksjonsrunde admin-row exception
# and the genuine 'nothing left to bill' cases). The CEO needs to trust
# that every Oppgave he sees in Notion will land on the draft.


@pytest.mark.asyncio
async def test_bulletproof_non_discipline_type_lands_on_draft(
    monkeypatch: pytest.MonkeyPatch,
):
    """A row whose Type is not in {Interiør, Eksteriør, Animasjon, Annet}
    — including blank Type — still produces an invoice line. Previously
    the engine silently dropped these, which was the CEO's prod bug.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        _oppgave(page_id="weird", name="Custom thing", discipline="", pris=5000.0),
        _oppgave(
            page_id="klargjor",
            name="Klargjøre modell",
            discipline="Klargjøre modell",
            pris=2000.0,
        ),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = await engine._build_line_items(
        eligible, invoice_type="oppstart", company_slug="probe"
    )
    assert sorted(l.oppgave_page_id for l in lines) == ["klargjor", "weird"]


def test_bulletproof_free_text_default_account_constant_exists():
    """Engine sends `incomeAccount = FIKEN_FREE_TEXT_INCOME_ACCOUNT` on
    free-text lines (when no Fiken product matches the Kategori). Without
    the default Fiken 400s the entire draft. 3020 = services @ 25% VAT.
    """
    from gb_automations.config import FIKEN_FREE_TEXT_INCOME_ACCOUNT

    assert FIKEN_FREE_TEXT_INCOME_ACCOUNT == "3020"


@pytest.mark.asyncio
async def test_bulletproof_negative_remainder_still_skipped(
    monkeypatch: pytest.MonkeyPatch,
):
    """The ONLY case the engine still silently skips is the over-billed
    one: slutt run where Pris was renegotiated down below Fakturert
    beløp. Skipping is intentional — Notion's Budsjett vs Fakturert sum
    rollups surface the discrepancy; the engine's job isn't to issue
    credit notes inline. Pin so a future change doesn't accidentally
    re-introduce a row with a kr 0 (or worse, negative) line.
    """
    monkeypatch.setattr(
        engine, "_resolve_kategori_to_product_id", _stub_resolver()
    )
    rows = [
        # Over-billed: oppstart booked 7500, then operator dropped Pris
        # to 5000. Slutt has remaining = -2500.
        _oppgave(
            page_id="overbill",
            pris=5000.0,
            billed_amount_nok=7500.0,
            fakturert_status=FAKTURERT_STATUS_50,
        ),
        # Clean slutt row for sanity (lands).
        _oppgave(page_id="ok", pris=5000.0, fakturert_status=FAKTURERT_STATUS_IKKE),
    ]
    eligible = engine._eligible_rows(rows, "slutt")
    lines = await engine._build_line_items(
        eligible, invoice_type="slutt", company_slug="probe"
    )
    # `ok` lands; over-billed row stays off the draft.
    assert [l.oppgave_page_id for l in lines] == ["ok"]


# ---------------------------------------------------------------------------
# Orphan-draft reconciliation — _filter_orphan_drafts (the pure predicate
# behind find_orphan_drafts). Resilience tooling for the duplicate-draft
# class of bug: a live Fiken draft with no active FikenInvoice audit row.
# ---------------------------------------------------------------------------


def _draft(draft_id: str, *, reference: str = "", uuid: str = "u") -> dict[str, Any]:
    return {
        "draftId": draft_id,
        "uuid": uuid,
        "ourReference": reference,
        "issueDate": "2026-06-22",
    }


def test_filter_orphan_drafts_flags_drafts_with_no_audit_row():
    drafts = [_draft("100"), _draft("200"), _draft("300")]
    # 200 is known/active (has an unsent audit row); 100 + 300 are orphans.
    known = {"200"}
    orphans = engine._filter_orphan_drafts(drafts, known)
    assert {o["draft_id"] for o in orphans} == {"100", "300"}


def test_filter_orphan_drafts_scopes_by_reference_when_title_given():
    drafts = [
        _draft("100", reference="Cinesuit AS"),
        _draft("200", reference="Annet Prosjekt"),
        _draft("300", reference=""),  # blank ref — unattributable
    ]
    orphans = engine._filter_orphan_drafts(
        drafts, set(), project_title="cinesuit as"  # casefold match
    )
    # Only the casefold-exact ourReference match survives; blank + other
    # project are left alone (never deleted by a project-scoped call).
    assert [o["draft_id"] for o in orphans] == ["100"]


def test_filter_orphan_drafts_unscoped_returns_all_orphans():
    drafts = [_draft("100", reference="x"), _draft("300", reference="")]
    orphans = engine._filter_orphan_drafts(drafts, set(), project_title=None)
    assert {o["draft_id"] for o in orphans} == {"100", "300"}


def test_filter_orphan_drafts_ignores_payload_without_draft_id():
    drafts = [{"uuid": "u", "ourReference": "x"}, _draft("100")]
    orphans = engine._filter_orphan_drafts(drafts, set())
    assert [o["draft_id"] for o in orphans] == ["100"]
