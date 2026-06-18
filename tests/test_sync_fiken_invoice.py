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
    rows = [
        _oppgave(page_id="a"),  # Ikke fakturert → ✓
        _oppgave(page_id="b", fakturert_status=FAKTURERT_STATUS_50),  # ✗
        _oppgave(page_id="c", fakturert_status=FAKTURERT_STATUS_FULL),  # ✗
        _oppgave(page_id="d", fakturert_status=FAKTURERT_STATUS_UTGAR),  # ✗
        _oppgave(page_id="e", discipline="Klargjøre modell"),  # ✗ not discipline
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    assert [row["id"] for row in eligible] == ["a"]


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


def test_eligible_rows_rejects_bad_invoice_type():
    with pytest.raises(ValueError, match="invoice_type must be"):
        engine._eligible_rows([], "midt")


# ---------------------------------------------------------------------------
# _row_billable_nok — discount, renegotiation, "nothing left" skip
# ---------------------------------------------------------------------------


def test_row_billable_oppstart_basic_50_percent():
    """No rabatt — to_bill and unit_price are the same kr 5000.
    Fiken receives unitPrice=5000, no discount field needed.
    """
    row = _oppgave(page_id="a", pris=10_000.0)
    to_bill, new_total, unit_price, discount_fraction = engine._row_billable_nok(
        row, "oppstart"
    )
    assert to_bill == 5000.0
    assert new_total == 5000.0
    assert unit_price == 5000.0
    assert discount_fraction == 0.0


def test_row_billable_oppstart_with_rabatt():
    """Rabatt 10% (Notion Percent format → 0.10) on 10000 NOK, oppstart:
        unit_price = Pris × 0.5 = 5000   (sent to Fiken)
        discount   = 10%                 (sent to Fiken)
        to_bill    = gross × 0.5 = 4500  (Notion ledger / audit trail)
    Fiken's UI shows "Pris kr 5000 / Rabatt 10% / Sum kr 4500" — the
    customer sees the discount on the printed invoice instead of just
    a pre-rabattert unit price.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.10)
    to_bill, new_total, unit_price, discount_fraction = engine._row_billable_nok(
        row, "oppstart"
    )
    assert to_bill == 4500.0
    assert new_total == 4500.0
    assert unit_price == 5000.0
    assert discount_fraction == 0.10


def test_row_billable_slutt_remainder_after_oppstart():
    """Oppstart booked 7500 (50% of original 15K). Pris then renegotiated
    down to 10000. Slutt run should bill 10000 − 7500 = 2500 (NOT 0,
    NOT the original remainder). Pre-rabatt unit price equals post-rabatt
    when Rabatt=0.
    """
    row = _oppgave(page_id="a", pris=10_000.0, billed_amount_nok=7500.0)
    to_bill, new_total, unit_price, discount_fraction = engine._row_billable_nok(
        row, "slutt"
    )
    assert to_bill == 2500.0
    assert new_total == 10_000.0
    assert unit_price == 2500.0
    assert discount_fraction == 0.0


def test_row_billable_slutt_with_rabatt_inverts_to_unit_price():
    """Slutt run, Rabatt 20%, fresh row (no prior oppstart): we want
    Fiken to print "Pris kr X / Rabatt 20% / Sum kr 8000" where
    post-rabatt Sum = 8000. Engine computes:
        gross     = 10000 × 0.8 = 8000
        to_bill   = 8000          (no prior billing)
        unit_price = 8000 / (1 − 0.20) = 10000   (the full undiscounted Pris)
    So Fiken displays Pris=10000, Rabatt=20%, Sum=8000.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.20)
    to_bill, new_total, unit_price, discount_fraction = engine._row_billable_nok(
        row, "slutt"
    )
    assert to_bill == 8000.0
    assert new_total == 8000.0
    assert unit_price == 10_000.0
    assert discount_fraction == 0.20


def test_row_billable_slutt_skips_when_remaining_zero_or_negative():
    """Renegotiation can push remaining ≤ 0 (operator dropped Pris below
    what was already invoiced). Skip cleanly — no negative line in Fiken.
    """
    row = _oppgave(page_id="a", pris=5000.0, billed_amount_nok=7500.0)
    assert engine._row_billable_nok(row, "slutt") is None


def test_row_billable_skips_missing_or_zero_price():
    row_no_price = _oppgave(page_id="a", pris=None)
    row_zero = _oppgave(page_id="b", pris=0.0)
    assert engine._row_billable_nok(row_no_price, "oppstart") is None
    assert engine._row_billable_nok(row_zero, "oppstart") is None


def test_row_billable_rabatt_uses_notion_percent_format():
    """Regression for the 4992-vs-5000 bug. Operator set Rabatt to 15%
    in Notion's Percent-formatted column; Notion's API returns the
    value as the fraction 0.15 (NOT 15). Engine must treat it as a
    fraction directly. Pris=10000 + Rabatt=0.15 + oppstart →
    gross=8500 → to_bill=4250.

    Earlier engine math divided the value by 100 again, producing
    `0.15/100 = 0.0015 = 0.15%` discount, with the line landing at
    NOK 4992 (`round(10000 × 0.9985 × 0.5)`) instead of the correct
    NOK 4250.
    """
    row = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=0.15)
    to_bill, new_total, unit_price, discount_fraction = engine._row_billable_nok(
        row, "oppstart"
    )
    assert to_bill == 4250.0
    assert new_total == 4250.0
    # Pre-rabatt: Pris × 0.5 = 5000. The customer sees Pris kr 5000,
    # Rabatt 15%, Sum kr 4250 — not just a pre-rabattert kr 4250.
    assert unit_price == 5000.0
    assert discount_fraction == 0.15


def test_row_billable_clamps_rabatt_to_valid_range():
    """Negative Rabatt collapses to 0 (no discount); above 1.0 (i.e.
    >100%) collapses to 1.0 and the row contributes 0. Defensive —
    Notion's number field has no built-in bounds, and an operator typing
    `150` into a Percent column gets stored as 1.5.
    """
    row_negative = _oppgave(page_id="a", pris=10_000.0, rabatt_fraction=-0.50)
    to_bill, _, _, discount_fraction = engine._row_billable_nok(
        row_negative, "oppstart"
    )
    assert to_bill == 5000.0  # rabatt clamped to 0 → full price
    assert discount_fraction == 0.0

    row_huge = _oppgave(page_id="b", pris=10_000.0, rabatt_fraction=1.5)
    assert engine._row_billable_nok(row_huge, "oppstart") is None  # gross=0


# ---------------------------------------------------------------------------
# _build_line_items — one line per Oppgave, NOK in øre
# ---------------------------------------------------------------------------


def test_build_line_items_one_line_per_oppgave():
    rows = [
        _oppgave(page_id=f"i{i}", name=f"Bilde {i}", pris=5000.0)
        for i in range(5)
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")
    assert len(lines) == 5
    assert {l.discipline for l in lines} == {"interior"}
    # No Kategori on these rows → product_name falls back to the Navn.
    assert sorted(l.product_name for l in lines) == [
        f"Bilde {i}" for i in range(5)
    ]
    # 50% of 5000 = 2500 NOK = 250 000 øre. No rabatt → unit_price ==
    # amount, discount_percent = 0.
    assert all(l.amount_nok_ore == 250_000 for l in lines)
    assert all(l.unit_price_nok_ore == 250_000 for l in lines)
    assert all(l.discount_percent == 0.0 for l in lines)
    # No kategori → default account.
    assert all(l.income_account == "3020" for l in lines)


def test_build_line_items_oppstart_with_rabatt_sends_undiscounted_unit_price():
    """With Rabatt=15%, the line carries:
        unit_price_nok_ore = round(Pris × 0.5) × 100 = 500_000  (kr 5000)
        discount_percent   = 15.0
        amount_nok_ore     = round(gross × 0.5) × 100 = 425_000 (kr 4250)
    so Fiken's printed invoice shows Pris/Rabatt/Sum instead of a
    pre-rabattert unit price.
    """
    rows = [_oppgave(page_id="a", name="Stue", pris=10_000.0, rabatt_fraction=0.15)]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")
    assert len(lines) == 1
    assert lines[0].unit_price_nok_ore == 500_000
    assert lines[0].discount_percent == 15.0
    assert lines[0].amount_nok_ore == 425_000


def test_build_line_items_slutt_with_renegotiation():
    """A row with billed_amount = 7500 (from a prior 50% of 15K Pris), Pris
    now 10000 → slutt line should be 2500 NOK (= 250_000 øre).
    """
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
    lines = engine._build_line_items(eligible, invoice_type="slutt")
    assert len(lines) == 1
    assert lines[0].amount_nok_ore == 250_000
    assert lines[0].new_billed_amount_nok == 10_000.0


def test_build_line_items_skips_rows_without_price():
    rows = [
        _oppgave(page_id="ok", name="Stue", pris=5000.0),
        _oppgave(page_id="no_price", name="Kjøkken", pris=None),
        _oppgave(page_id="zero", name="Bad", pris=0.0),
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")
    assert [l.oppgave_page_id for l in lines] == ["ok"]


def test_build_line_items_skips_zero_remainder_on_slutt():
    """Renegotiation leaves nothing to bill → no line for that row."""
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
    lines = engine._build_line_items(eligible, invoice_type="slutt")
    assert [l.oppgave_page_id for l in lines] == ["b"]
    assert lines[0].amount_nok_ore == 500_000  # full 5000 NOK


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
# _resolve_kategori_and_account — first Kategori label drives the account
# ---------------------------------------------------------------------------


def test_resolve_kategori_routes_tjeneste_to_3020():
    """Known kategori 'Næring - Interiør' is a tjeneste → account 3020."""
    row = _oppgave(page_id="a", kategori=["Næring - Interiør"])
    kategori, account = engine._resolve_kategori_and_account(row)
    assert kategori == "Næring - Interiør"
    assert account == "3020"


def test_resolve_kategori_routes_vare_to_3000():
    """Known kategori 'Print' is a vare → account 3000."""
    row = _oppgave(page_id="a", kategori=["Print"])
    kategori, account = engine._resolve_kategori_and_account(row)
    assert kategori == "Print"
    assert account == "3000"


def test_resolve_kategori_picks_first_when_multiple_selected():
    """Multi-select can carry several labels — engine picks the first.
    Notion preserves operator's selection order, so the first label is
    the operator's primary intent.
    """
    row = _oppgave(page_id="a", kategori=["Næring - Interiør", "Print"])
    kategori, account = engine._resolve_kategori_and_account(row)
    assert kategori == "Næring - Interiør"
    assert account == "3020"


def test_resolve_kategori_unknown_label_defaults_to_3020():
    """An unmapped kategori falls back to FIKEN_DEFAULT_INCOME_ACCOUNT
    (3020) so we never block a draft on a missing dict entry — the
    operator sees the WARN in logs and adds the kategori to the map.
    """
    row = _oppgave(page_id="a", kategori=["Bolig - Opparbeide Modell"])
    kategori, account = engine._resolve_kategori_and_account(row)
    assert kategori == "Bolig - Opparbeide Modell"
    assert account == "3020"


def test_resolve_kategori_blank_returns_none_and_default():
    """Row with no kategori labels → (None, default account)."""
    row = _oppgave(page_id="a")  # kategori defaults to []
    kategori, account = engine._resolve_kategori_and_account(row)
    assert kategori is None
    assert account == "3020"


# ---------------------------------------------------------------------------
# _build_line_items — Kategori flows through to the line dataclass
# ---------------------------------------------------------------------------


def test_build_line_items_threads_kategori_into_product_name_and_account():
    """End-to-end through _build_line_items: a row with Kategori
    'Næring - Interiør' produces a line whose product_name = the
    Kategori label, income_account = 3020, comment = "Navn - Beskrivelse",
    and the amount fields are unaffected.
    """
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
    lines = engine._build_line_items(eligible, invoice_type="oppstart")
    assert len(lines) == 1
    line = lines[0]
    assert line.product_name == "Næring - Interiør"
    assert line.comment == "Kjøkken - Hovedbilde dag"
    assert line.income_account == "3020"
    # Unchanged amount path (50% of 10000 = 5000 NOK = 500_000 øre).
    assert line.unit_price_nok_ore == 500_000
    assert line.amount_nok_ore == 500_000


def test_build_line_items_routes_print_to_3000():
    """Kategori 'Print' → income_account 3000 (vare)."""
    rows = [
        _oppgave(
            page_id="a",
            name="Plakat A1",
            description="20 stk",
            pris=4000.0,
            kategori=["Print"],
        )
    ]
    eligible = engine._eligible_rows(rows, "oppstart")
    lines = engine._build_line_items(eligible, invoice_type="oppstart")
    assert len(lines) == 1
    assert lines[0].product_name == "Print"
    assert lines[0].income_account == "3000"


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
