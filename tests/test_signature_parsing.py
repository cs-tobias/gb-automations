"""Tests for utils/signature_parsing.py using real-world Goldbox signatures."""

from gb_automations.utils.signature_parsing import SignatureFields, parse_signature


# Captured verbatim from a Thon Eiendom email — the canonical "complete"
# Norwegian business signature: name, title, phone, email, then a blank,
# then company + street/postal, then URLs.
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
# no address. The parser falls back to the URL stem for the company.
PETTER_SIGNATURE = """\
Petter Burhol
Daglig leder


(+47) 934 87 481
petter@goldbox.no
www.goldbox.no
"""


def test_thon_signature_extracts_all_fields():
    result = parse_signature(THON_SIGNATURE, sender_name="Ingeborg Kvamme Skar")
    assert result == SignatureFields(
        title="Markedsansvarlig",
        address="Stenersgata 2A, 0184 Oslo",
        company="Thon Eiendom",
    )


def test_petter_signature_falls_back_to_url_stem_for_company():
    result = parse_signature(PETTER_SIGNATURE, sender_name="Petter Burhol")
    assert result.title == "Daglig leder"
    assert result.address is None
    # No explicit company line — pulled from www.goldbox.no
    assert result.company == "Goldbox"


def test_empty_signature_returns_all_none():
    result = parse_signature("", sender_name="Whoever")
    assert result == SignatureFields(None, None, None)


def test_whitespace_only_signature_returns_all_none():
    result = parse_signature("   \n  \n", sender_name="Whoever")
    assert result == SignatureFields(None, None, None)


def test_without_sender_name_still_finds_title_via_phone_fallback():
    # Even without sender_name we can locate the title as the line above
    # the first phone/email line, and the address still parses. Company
    # detection without a name hint is unreliable (the name line itself
    # looks company-shaped), so we don't assert on company here — real
    # callers always pass sender_name.
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


def test_company_skips_address_and_url_lines():
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
    assert result.company == "Firma AS"
