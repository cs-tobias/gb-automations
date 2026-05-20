"""Tests for utils/signature_parsing.py using real-world Goldbox signatures."""

from gb_automations.utils.signature_parsing import (
    SignatureFields,
    _is_address_line,
    clean_phone_line,
    clean_title_line,
    parse_signature,
)

# Captured verbatim from a Thon Eiendom email — the canonical "complete"
# Norwegian business signature: name, title, phone, email, then a blank,
# then company + street/postal, then URLs. (Company is no longer parsed —
# it comes from the email-domain stem — but the block still exercises title
# and address extraction.)
THON_SIGNATURE = """\
Med vennlig hilsen / Best Regards
Ingeborg Kvamme Skar
Markedsansvarlig
+47 977 99 616
ingeborg.skar@olavthon.no



Thon Eiendom
Stenersgata 2A, 0184 Oslo

thoneiendom.no
olavthon.no
"""

# Petter's signature in the wild: no Mvh sign-off, no explicit company line,
# no address.
PETTER_SIGNATURE = """\
Petter Burhol
Daglig leder


(+47) 934 87 481
petter@goldbox.no
www.goldbox.no
"""


def test_thon_signature_extracts_title_and_address():
    result = parse_signature(THON_SIGNATURE, sender_name="Ingeborg Kvamme Skar")
    assert result == SignatureFields(
        title="Markedsansvarlig",
        address="Stenersgata 2A, 0184 Oslo",
    )


def test_petter_signature_extracts_title_no_address():
    result = parse_signature(PETTER_SIGNATURE, sender_name="Petter Burhol")
    assert result.title == "Daglig leder"
    assert result.address is None


def test_empty_signature_returns_all_none():
    result = parse_signature("", sender_name="Whoever")
    assert result == SignatureFields(None, None)


def test_whitespace_only_signature_returns_all_none():
    result = parse_signature("   \n  \n", sender_name="Whoever")
    assert result == SignatureFields(None, None)


def test_without_sender_name_still_finds_title_via_phone_fallback():
    # Even without sender_name we can locate the title as the line above
    # the first phone/email line, and the address still parses.
    result = parse_signature(THON_SIGNATURE, sender_name=None)
    assert result.title == "Markedsansvarlig"
    assert result.address == "Stenersgata 2A, 0184 Oslo"


def test_title_line_with_digits_is_rejected():
    # If the line below the name is a phone number, no title is reported
    # (rather than misreporting the phone as a title).
    sig = "Anne Hansen\n+47 22 33 44 55\nanne@example.no\n"
    result = parse_signature(sig, sender_name="Anne Hansen")
    assert result.title is None


def test_address_without_street_line_above_returns_postal_only():
    sig = """\
Kari Nordmann
Konsulent
kari@example.no

0150 Oslo
example.no
"""
    result = parse_signature(sig, sender_name="Kari Nordmann")
    # No street above the postal line → just the postal/city.
    assert result.address == "0150 Oslo"


def test_full_signature_extracts_title_and_address():
    sig = """\
Ola Nordmann
Designer
+47 999 88 777
ola@firma.no

Firma AS
Storgata 1, 0150 Oslo
firma.no
"""
    result = parse_signature(sig, sender_name="Ola Nordmann")
    assert result.title == "Designer"
    assert result.address == "Storgata 1, 0150 Oslo"


def test_address_does_not_prepend_phone_line_as_street():
    """A phone line above the postal line must not be glued on as the street.
    Regression: `Mob.: +47 90178028, 2212 Kongsvinger` was the buggy output."""
    sig = """\
Lasse Ellingsen
Senterleder
lasse@kongssenteret.no
Mob.: +47 90178028
Brugata 15, 2212 Kongsvinger
"""
    result = parse_signature(sig, sender_name="Lasse Ellingsen")
    assert result.address == "Brugata 15, 2212 Kongsvinger"


# ============================================================
# Norwegian-cities address fallback
# ============================================================


def test_is_address_line_matches_postal_code_form():
    assert _is_address_line("Brugata 15, 2212 Kongsvinger") is True


def test_is_address_line_matches_known_city_when_postal_regex_misses():
    # "NO0258" defeats the \b\d{4} postal regex; the city checklist catches it.
    assert _is_address_line("a: Parkveien 37 | NO0258 Oslo | Norway") is True


def test_is_address_line_false_for_plain_text():
    assert _is_address_line("Daglig leder") is False


# ============================================================
# Locator-line cleaners (the LLM-primary path)
# ============================================================


def test_clean_phone_line_strips_label_and_normalizes():
    assert clean_phone_line("m: +47 93 88 32 01") == "+47 93 88 32 01"
    assert clean_phone_line("Mob.: +47 90178028") == "+47 90178028"


def test_clean_phone_line_returns_none_on_no_number():
    assert clean_phone_line("Senterleder") is None
    assert clean_phone_line(None) is None


def test_clean_title_line_strips_trailing_company_when_known():
    assert clean_title_line("Daglig leder| NIMREM", "Nimrem") == "Daglig leder"


def test_clean_title_line_keeps_dual_role_when_company_does_not_match():
    # No company suffix to strip → the combined role survives verbatim.
    assert clean_title_line("Partner / Daglig leder", "Goldbox") == "Partner / Daglig leder"


def test_clean_title_line_plain_title_unchanged():
    assert clean_title_line("Senterleder", "Kongssenteret") == "Senterleder"
    assert clean_title_line("Interiørarkitekt MA", sender_name="Hedda Torgersen") == (
        "Interiørarkitekt MA"
    )
    assert clean_title_line(None, "X") is None


def test_clean_title_line_rejects_sender_name():
    # Charlotte's signature is just name + phone — the LLM may point at the name.
    assert clean_title_line("Charlotte Hagemoen", sender_name="Charlotte Hagemoen") is None


def test_clean_title_line_rejects_url_email_phone():
    assert clean_title_line("metropolis.no") is None
    assert clean_title_line("www.goldbox.no") is None
    assert clean_title_line("ht@metropolis.no") is None
    assert clean_title_line("+47 93 48 44 35") is None
