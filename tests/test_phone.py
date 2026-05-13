"""Tests for utils/phone.py — Norwegian + international, mobile preference."""

from gb_automations.utils.phone import extract_phone


def test_extract_phone_returns_none_for_empty():
    assert extract_phone("") is None
    assert extract_phone("   ") is None


def test_extract_phone_returns_none_when_no_number():
    assert extract_phone("Just some text without numbers") is None


def test_extract_phone_finds_norwegian_8_digit_local():
    assert extract_phone("Ring 999 88 777 for spørsmål") == "999 88 777"


def test_extract_phone_finds_norwegian_8_digit_no_spaces():
    assert extract_phone("Tlf 99988777") == "99988777"


def test_extract_phone_finds_international_with_plus():
    # "+47 999 88 777" gets normalized — runs of separators collapsed
    result = extract_phone("Phone: +47 999 88 777")
    assert result is not None
    assert "+47" in result
    assert "999" in result


def test_extract_phone_finds_international_with_dashes():
    result = extract_phone("Call +1-415-555-1234")
    assert result is not None
    assert result.startswith("+1")


def test_extract_phone_normalizes_mixed_separators_to_single_space():
    # Regex requires single separators between digit groups; output normalizes to spaces.
    assert extract_phone("Tlf: 999-88.777") == "999 88 777"


def test_extract_phone_prefers_mobile_when_both_present():
    text = "Office: 22 33 44 55\nMob: 999 88 777"
    result = extract_phone(text)
    assert result == "999 88 777"


def test_extract_phone_recognizes_mobil_label():
    text = "Tlf: 22 33 44 55\nMobil: 999 88 777"
    assert extract_phone(text) == "999 88 777"


def test_extract_phone_recognizes_m_colon_label():
    text = "T: 22 33 44 55\nM: 999 88 777"
    assert extract_phone(text) == "999 88 777"


def test_extract_phone_falls_back_to_first_match_when_no_mobile_marker():
    text = "999 88 777 and also 11 22 33 44"
    assert extract_phone(text) == "999 88 777"


def test_extract_phone_rejects_non_8_digit_norwegian_runs():
    # A 7-digit run shouldn't be matched as a Norwegian local number
    result = extract_phone("Just 123 456 7 here")
    assert result is None


def test_extract_phone_handles_signature_block():
    sig = "Mvh\nTobias Eek\nGoldbox AS\nMob: 999 88 777\ntobias@goldbox.no"
    assert extract_phone(sig) == "999 88 777"
