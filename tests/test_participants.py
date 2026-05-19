"""Tests for utils/participants.py — covers parsing, internal-domain checks, company extraction."""

import os
from unittest.mock import patch

import pytest

from gb_automations.utils.participants import (
    company_from_domain,
    extract_email,
    extract_name,
    find_sender_email,
    is_free_mail_domain,
    is_internal,
    parse_participant,
    strict_email_or_empty,
)

# ============================================================
# extract_email
# ============================================================


def test_extract_email_pulls_from_brackets():
    assert extract_email("Tobias <tobias@x.com>") == "tobias@x.com"


def test_extract_email_returns_trimmed_field_when_no_brackets():
    assert extract_email("  bare@x.com  ") == "bare@x.com"


# ============================================================
# extract_name
# ============================================================


def test_extract_name_strips_quotes():
    assert extract_name('"Tobias Eek" <tobias@x.com>') == "Tobias Eek"


def test_extract_name_strips_whitespace():
    assert extract_name("  Tobias Eek   <tobias@x.com>") == "Tobias Eek"


def test_extract_name_falls_back_to_local_part_when_no_display_name():
    assert extract_name("bare@x.com") == "bare"


# ============================================================
# parse_participant
# ============================================================


def test_parse_participant_returns_none_for_empty():
    assert parse_participant("") is None
    assert parse_participant("   ") is None


def test_parse_participant_returns_none_when_no_email():
    assert parse_participant("just a name with no email") is None


def test_parse_participant_lowercases_email():
    p = parse_participant("Tobias <Tobias@Example.COM>")
    assert p is not None
    assert p.email == "tobias@example.com"


def test_parse_participant_keeps_real_name():
    p = parse_participant("Petter Burhol <petter@goldbox.no>")
    assert p is not None
    assert p.name == "Petter Burhol"
    assert p.email == "petter@goldbox.no"


def test_parse_participant_drops_generic_aliases():
    # "post" is in _GENERIC_LOCAL_NAMES — when used as the display name it's noise
    p = parse_participant("post <post@goldbox.no>")
    assert p is not None
    assert p.name is None  # name dropped because it's a generic alias
    assert p.email == "post@goldbox.no"


def test_parse_participant_drops_name_equal_to_email():
    p = parse_participant("tobias@x.com <tobias@x.com>")
    assert p is not None
    assert p.name is None


def test_parse_participant_drops_name_equal_to_local_part():
    p = parse_participant("tobias <tobias@x.com>")
    assert p is not None
    assert p.name is None


def test_parse_participant_collapses_internal_whitespace_in_name():
    p = parse_participant('"Tobias    Eek"  <tobias@x.com>')
    assert p is not None
    assert p.name == "Tobias Eek"


# ============================================================
# is_internal
# ============================================================


def test_is_internal_matches_exact_email():
    with patch.dict(os.environ, {"INTERNAL_EMAILS_OR_DOMAINS": "tobias@goldbox.no"}):
        assert is_internal("tobias@goldbox.no") is True
        assert is_internal("petter@goldbox.no") is False


def test_is_internal_matches_domain_suffix():
    with patch.dict(os.environ, {"INTERNAL_EMAILS_OR_DOMAINS": "goldbox.no"}):
        assert is_internal("anyone@goldbox.no") is True
        assert is_internal("anyone@example.com") is False


def test_is_internal_handles_mixed_list():
    val = "tobias@goldbox.no, post@goldbox.no, internal.example.com"
    with patch.dict(os.environ, {"INTERNAL_EMAILS_OR_DOMAINS": val}):
        assert is_internal("tobias@goldbox.no") is True
        assert is_internal("anyone@internal.example.com") is True
        assert is_internal("petter@goldbox.no") is False  # not in exact list, no domain match
        assert is_internal("external@example.com") is False


def test_is_internal_is_case_insensitive():
    with patch.dict(os.environ, {"INTERNAL_EMAILS_OR_DOMAINS": "Goldbox.NO"}):
        assert is_internal("ToBiAs@goldbox.no") is True


def test_is_internal_handles_unset_env_var():
    with patch.dict(os.environ, {}, clear=True):
        # default is empty → nothing is internal
        assert is_internal("anyone@anywhere.com") is False


def test_is_internal_falls_back_to_workspace_domain(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("INTERNAL_EMAILS_OR_DOMAINS", raising=False)
    monkeypatch.setenv("WORKSPACE_DOMAIN", "goldbox.no")
    assert is_internal("petter@goldbox.no") is True
    assert is_internal("client@example.com") is False


def test_is_internal_explicit_var_takes_precedence(monkeypatch: pytest.MonkeyPatch):
    """If both env vars are set, the explicit one wins — lets ops override
    the workspace-domain default (e.g. 'team@otherdomain.com is also us')."""
    monkeypatch.setenv("INTERNAL_EMAILS_OR_DOMAINS", "team@cinesuit.com")
    monkeypatch.setenv("WORKSPACE_DOMAIN", "goldbox.no")
    assert is_internal("team@cinesuit.com") is True
    # WORKSPACE_DOMAIN should NOT be consulted when the explicit list is set.
    assert is_internal("petter@goldbox.no") is False


# ============================================================
# company_from_domain
# ============================================================


def test_company_from_domain_capitalizes_main_label():
    assert company_from_domain("ht@metropolis.no") == "Metropolis"


def test_company_from_domain_handles_subdomain():
    assert company_from_domain("person@sub.company.com") == "Company"


def test_company_from_domain_handles_single_label():
    # Edge case: an unusual local-only address
    assert company_from_domain("u@localhost") == "Localhost"


def test_company_from_domain_returns_empty_for_no_at():
    assert company_from_domain("no-at-sign") == ""


# ============================================================
# find_sender_email
# ============================================================


def test_find_sender_email_pulls_from_brackets_and_lowercases():
    assert find_sender_email("Tobias <Tobias@Example.COM>") == "tobias@example.com"


def test_find_sender_email_handles_bare_email():
    assert find_sender_email("  Bare@X.COM  ") == "bare@x.com"


# ============================================================
# strict_email_or_empty — does NOT fall back to lowercased bare string
# ============================================================


def test_strict_email_or_empty_returns_email_when_present():
    assert strict_email_or_empty("Tobias <Tobias@Example.COM>") == "tobias@example.com"


def test_strict_email_or_empty_handles_bare_email():
    assert strict_email_or_empty("bare@x.com") == "bare@x.com"


def test_strict_email_or_empty_returns_empty_for_bare_name():
    # The whole reason this helper exists: LLM can return just a display name
    # like 'Petter Burhol' with no email. Don't write that into a Notion
    # `email`-typed property.
    assert strict_email_or_empty("Petter Burhol") == ""


def test_strict_email_or_empty_returns_empty_for_empty_string():
    assert strict_email_or_empty("") == ""


def test_strict_email_or_empty_returns_empty_for_no_at_sign():
    assert strict_email_or_empty("not an email") == ""


# ============================================================
# is_free_mail_domain
# ============================================================


def test_is_free_mail_domain_recognizes_gmail():
    assert is_free_mail_domain("gmail.com") is True


def test_is_free_mail_domain_recognizes_outlook_hotmail_icloud():
    assert is_free_mail_domain("outlook.com") is True
    assert is_free_mail_domain("hotmail.com") is True
    assert is_free_mail_domain("hotmail.no") is True
    assert is_free_mail_domain("icloud.com") is True


def test_is_free_mail_domain_recognizes_norwegian_consumer_isps():
    assert is_free_mail_domain("online.no") is True
    assert is_free_mail_domain("yahoo.no") is True


def test_is_free_mail_domain_case_insensitive():
    assert is_free_mail_domain("GMAIL.COM") is True
    assert is_free_mail_domain("Outlook.Com") is True


def test_is_free_mail_domain_rejects_real_company_domains():
    assert is_free_mail_domain("goldbox.no") is False
    assert is_free_mail_domain("metropolis.no") is False
    assert is_free_mail_domain("thon.no") is False


def test_is_free_mail_domain_rejects_empty_string():
    assert is_free_mail_domain("") is False
