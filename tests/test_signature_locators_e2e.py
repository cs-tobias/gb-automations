"""End-to-end of the locator path on the two real signatures that motivated it.

Flow: mocked Ollama returns verbatim located lines → `classify_signature`
yields `SignatureLocators` → `_resolve_located_line` resolves each line against
the body → the deterministic cleaners produce title/phone. Address is NOT an
LLM locator — it comes from the regex `parse_signature`, which joins the
two-line street+postal form. This pins the whole pipeline without a live LLM.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from gb_automations.clients import llm as llm_client
from gb_automations.clients.llm import SignatureLocators
from gb_automations.sync.sync_thread import _resolve_located_line
from gb_automations.utils.participants import company_from_domain
from gb_automations.utils.signature_parsing import (
    clean_phone_line,
    clean_title_line,
    parse_signature,
)

SIGNATURE_A = """\
Hei Petter,
Se bilde to 🙂
med vennlig hilsen
Caspar Vinje Hagland
Daglig leder| NIMREM
m: +47 93 88 32 01
e: cvh@nimrem.no
a: Parkveien 37 | NO0258 Oslo | Norway
"""

SIGNATURE_B = """\
Med vennlig hilsen
Lasse Ellingsen
Senterleder
lasse@kongssenteret.no
Mob.: +47 90178028
Brugata 15, 2212 Kongsvinger
www.kongssenteret.no
"""


def _mock_response(locators: dict) -> tuple[str, dict]:
    return (json.dumps(locators), {"done": True})


def _classify(body: str, locators: dict, sender_name: str) -> SignatureLocators:
    with patch.object(
        llm_client, "_chat_streaming", new=AsyncMock(return_value=_mock_response(locators))
    ):
        return asyncio.run(llm_client.classify_signature(body, sender_name=sender_name))


def _resolve_and_clean(body: str, loc: SignatureLocators, sender_email: str, sender_name: str):
    known_company = company_from_domain(sender_email)
    title = clean_title_line(
        _resolve_located_line(body, loc.title_line), known_company, sender_name=sender_name
    )
    phone = clean_phone_line(_resolve_located_line(body, loc.phone_line))
    address = parse_signature(body, sender_name=sender_name).address
    return title, phone, address


def test_signature_a_full_pipeline():
    loc = _classify(
        SIGNATURE_A,
        {
            "title_line": "Daglig leder| NIMREM",
            "phone_line": "m: +47 93 88 32 01",
            "signature_first_line": "Caspar Vinje Hagland",
        },
        sender_name="Caspar Vinje Hagland",
    )
    title, phone, address = _resolve_and_clean(
        SIGNATURE_A, loc, "cvh@nimrem.no", "Caspar Vinje Hagland"
    )
    assert title == "Daglig leder"
    assert phone == "+47 93 88 32 01"
    # Regex address: street + postal joined; "NO0258 Oslo" recovered via cities.
    assert address == "Parkveien 37 | NO0258 Oslo | Norway"
    assert loc.signature_first_line == "Caspar Vinje Hagland"


def test_signature_b_full_pipeline():
    loc = _classify(
        SIGNATURE_B,
        {
            "title_line": "Senterleder",
            "phone_line": "Mob.: +47 90178028",
            "signature_first_line": "Med vennlig hilsen",
        },
        sender_name="Lasse Ellingsen",
    )
    title, phone, address = _resolve_and_clean(
        SIGNATURE_B, loc, "lasse@kongssenteret.no", "Lasse Ellingsen"
    )
    assert title == "Senterleder"
    assert phone == "+47 90178028"
    assert address == "Brugata 15, 2212 Kongsvinger"
    assert loc.signature_first_line == "Med vennlig hilsen"
