"""Tests for notion.task_discipline — reads the Oppgaver `Type` property.

Goldbox runs `Type` as a multi_select but only ever sets one label per row, so
task_discipline must read BOTH the single `select` shape and the `multi_select`
shape, returning the first RECOGNIZED discipline (falling back to the first
option when none is recognized, so the caller's own gate decides).
"""

from __future__ import annotations

from gb_automations.clients.notion import task_discipline
from gb_automations.config import OPPGAVER_PROPS

_TYPE = OPPGAVER_PROPS["discipline"]


def _page(type_prop: dict) -> dict:
    return {"properties": {_TYPE: type_prop}}


def test_single_select_returns_name():
    page = _page({"select": {"name": "Eksteriør"}})
    assert task_discipline(page) == "Eksteriør"


def test_single_select_empty_returns_none():
    assert task_discipline(_page({"select": None})) is None
    assert task_discipline(_page({})) is None


def test_multi_select_single_label_returns_it():
    page = _page({"multi_select": [{"name": "Interiør"}]})
    assert task_discipline(page) == "Interiør"


def test_multi_select_picks_first_recognized_discipline():
    # Even if a non-discipline label comes first, return the real discipline.
    page = _page(
        {"multi_select": [{"name": "Korreksjonsrunde"}, {"name": "Animasjon"}]}
    )
    assert task_discipline(page) == "Animasjon"


def test_multi_select_two_disciplines_returns_first():
    # Goldbox won't do this, but if two disciplines are set, the first wins.
    page = _page({"multi_select": [{"name": "Interiør"}, {"name": "Eksteriør"}]})
    assert task_discipline(page) == "Interiør"


def test_multi_select_no_recognized_falls_back_to_first():
    page = _page({"multi_select": [{"name": "Klargjøre modell"}]})
    assert task_discipline(page) == "Klargjøre modell"


def test_multi_select_empty_returns_none():
    assert task_discipline(_page({"multi_select": []})) is None


def test_missing_type_property_returns_none():
    assert task_discipline({"properties": {}}) is None
    assert task_discipline({}) is None
