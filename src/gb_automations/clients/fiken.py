"""Async Fiken API v2 REST client.

Mirrors the shape of `clients/toggl.py` — same httpx + retry pattern, same
event-hook logging, same `_raise_for_status` ergonomics. Auth is a Bearer
header carrying a Personal API Token (created at *Rediger konto → Sikkerhet
→ Personlige API-nøkler* in Fiken's UI).

Reference: https://api.fiken.no/api/v2/docs/ (Swagger — source of truth).

Phase B starts with only what the invoice-creation engine needs end-to-end:
    - whoami()                    — `GET /user` (smoke test)
    - list_companies()            — `GET /companies` (bootstrap helper)
    - list_contacts(customer=True) — to resolve a customer name → contactId
    - list_products() / upsert_product() — per-discipline product mirror
    - create_invoice_draft()      — `POST /companies/{slug}/invoices/drafts`

Phase C will add list_invoices / list_offers / get_invoice for the poller.
Don't speculatively add methods.

Two known unknowns are pinned by the research doc and validated against
real payloads via `GET /debug/fiken/invoice/{id}`:

  - REFERENCE_FIELD — the Fiken payload key whose value the existing Make
    automation matches the Notion project name against. Defaults to
    "ourReference" (Swagger's most likely candidate); the engine inspects
    Fiken's response on every draft and logs a WARN if the field is missing.
  - VAT_TYPE_25_PCT — the enum value Fiken expects for standard 25% NO
    VAT on B2B services. Defaults to "HIGH" (Fiken's documented "HIGH" =
    25% sats); switch to whatever the live payload shows.

When either turns out wrong, fix the constant here — no other file
references the strings.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)


FIKEN_API_BASE = "https://api.fiken.no/api/v2"
# Fiken's docs note "one concurrent request per token"; with the single-
# worker queue we serialize naturally, so the timeout just covers slow
# responses (account-level reads can take a few seconds on first call).
_HTTP_TIMEOUT = 30.0

# See module docstring for the rationale on these two constants.
REFERENCE_FIELD = "ourReference"
VAT_TYPE_25_PCT = "HIGH"


class FikenAPIError(RuntimeError):
    """Fiken HTTP error with body attached for diagnosis."""

    def __init__(self, response: httpx.Response):
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text}
        message = (
            f"Fiken {response.status_code} "
            f"{response.request.method} {response.url}: {body}"
        )
        super().__init__(message)
        self.status_code = response.status_code
        self.body = body

    def is_stale_object(self) -> bool:
        """True if the error looks like 'the thing you asked about is gone'.

        Used by sync engines to evict cached Fiken ids and re-create on next
        pass — mirrors `NotionAPIError.is_stale_object`. 404 is the main
        signal; 410 also appears on hard-deleted objects.
        """
        return self.status_code in (404, 410)


def _raise_for_status(response: httpx.Response) -> None:
    if response.is_success:
        return
    raise FikenAPIError(response)


async def _with_retries(
    operation: Callable[[], Awaitable[httpx.Response]],
    *,
    op_name: str,
    max_attempts: int = 3,
) -> httpx.Response:
    """Exponential backoff on transient httpx errors + 5xx + 429.

    Fiken has no documented rate-limit beyond "one concurrent request per
    token"; 429 is treated as transient out of caution. The single-worker
    queue means we don't actually run into concurrent calls today.
    """
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            response = await operation()
        except (
            httpx.ReadTimeout,
            httpx.ConnectTimeout,
            httpx.RemoteProtocolError,
        ) as err:
            last_err = err
            if attempt + 1 >= max_attempts:
                break
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "fiken %s attempt %d failed (%s); retrying in %.1fs",
                op_name,
                attempt + 1,
                type(err).__name__,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        if (
            response.status_code == 429
            or 500 <= response.status_code < 600
        ) and attempt + 1 < max_attempts:
            backoff = 1.0 if attempt == 0 else 4.0
            logger.warning(
                "fiken %s attempt %d got %d; retrying in %.1fs",
                op_name,
                attempt + 1,
                response.status_code,
                backoff,
            )
            await asyncio.sleep(backoff)
            continue
        return response
    assert last_err is not None
    raise last_err


async def _log_request(request: httpx.Request) -> None:
    logger.debug("fiken → %s %s", request.method, request.url.path)


async def _log_response(response: httpx.Response) -> None:
    logger.debug(
        "fiken ← %d %s %s",
        response.status_code,
        response.request.method,
        response.request.url.path,
    )


async def _client() -> httpx.AsyncClient:
    """Build an httpx client pre-loaded with auth + logging hooks.

    Reads the token from `settings.fiken_api_token` at call time (not module
    import) so late-bound env updates (e.g. the bootstrap script) work.
    """
    from gb_automations.config import settings

    if not settings.fiken_api_token:
        raise RuntimeError(
            "FIKEN_API_TOKEN is not configured. Create a Personal API Token "
            "at *Rediger konto → Sikkerhet → Personlige API-nøkler* in "
            "Fiken's UI and paste into .env."
        )
    return httpx.AsyncClient(
        base_url=FIKEN_API_BASE,
        timeout=_HTTP_TIMEOUT,
        headers={
            "Authorization": f"Bearer {settings.fiken_api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        event_hooks={"request": [_log_request], "response": [_log_response]},
    )


# ============================================================
# Reads — smoke tests + customer/product lookups
# ============================================================


async def whoami() -> dict[str, Any]:
    """`GET /user` — returns the authenticated Fiken user. Smoke test for
    auth chain: token valid, Bearer header correct, Fiken API reachable.
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get("/user"), op_name="whoami"
        )
        _raise_for_status(response)
        return response.json()


async def list_companies() -> list[dict[str, Any]]:
    """`GET /companies` — every company the token can reach.

    Used by the bootstrap to discover the `slug` to put in
    FIKEN_COMPANY_SLUG. Single-tenant Goldbox normally returns one entry.
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get("/companies"), op_name="list_companies"
        )
        _raise_for_status(response)
        return _unwrap_list(response.json())


async def create_contact(
    company_slug: str,
    *,
    name: str,
    organization_number: str | None = None,
    email: str | None = None,
    customer: bool = True,
) -> dict[str, Any]:
    """`POST /companies/{slug}/contacts` — create a new contact.

    Used by the invoice-creation engine when the Notion-side Orgnr
    doesn't match any existing Fiken customer: the engine creates a
    new contact on the fly with whatever fields it can read off the
    Notion Kunder row (name + Orgnr; address/email get filled in
    Fiken's UI later).

    `customer=True` flags the contact as billable (vs supplier-only),
    so it shows up in /contacts?customer=true on next list.

    Response shape mirrors `create_product`: Fiken usually returns
    201 with empty body + Location header carrying the new contact's
    URL. We parse the id from there and return a dict with contactId
    populated so the caller never has to special-case.
    """
    body: dict[str, Any] = {"name": name, "customer": customer}
    if organization_number:
        body["organizationNumber"] = organization_number
    if email:
        body["email"] = email

    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/companies/{company_slug}/contacts", json=body
            ),
            op_name="create_contact",
        )
        _raise_for_status(response)
        try:
            data = response.json()
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
        location = response.headers.get("Location") or ""
        contact_id = location.rsplit("/", 1)[-1] if location else ""
        return {
            "contactId": contact_id,
            "name": name,
            "organizationNumber": organization_number,
            "Location": location,
        }


def _unwrap_list(payload: Any) -> list[dict[str, Any]]:
    """Defensive coercion for Fiken's list responses.

    Fiken returns a plain JSON array `[{...}, ...]` on list endpoints.
    (Earlier we thought it returned `{"value": [...], "Count": N}`, but
    that was PowerShell's `Invoke-RestMethod | ConvertTo-Json` wrapping
    arrays — confirmed against raw `curl` showing `[`.) The helper kept
    for safety against future Fiken response-shape changes; returns []
    on anything we don't recognize so the caller never crashes on a
    surprise.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        items = payload.get("value")
        if isinstance(items, list):
            return items
    return []


async def list_contacts(
    company_slug: str, *, customer: bool = True
) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/contacts?customer=true` — paginated.

    Returns every contact flagged as a customer (the engine resolves a
    Notion project's customer by `organizationNumber`).

    Fiken paginates with `page` STARTING AT 0 (not 1 — verified
    empirically via the `fiken-api-page` response header). Sending
    page=1 returns an empty array when there are fewer than 100
    results, which silently broke duplicate-detection (engine saw no
    matches → created a fresh contact every run, eventually 7
    duplicates of the same Cinesuit). pageSize=100 is Fiken's
    documented max.
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            response = await _with_retries(
                lambda p=page: client.get(
                    f"/companies/{company_slug}/contacts",
                    params={
                        "customer": "true" if customer else "false",
                        "page": p,
                        "pageSize": 100,
                    },
                ),
                op_name="list_contacts",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


async def get_contact(company_slug: str, contact_id: str) -> dict[str, Any] | None:
    """`GET /companies/{slug}/contacts/{id}` — single contact lookup.

    Used by Phase C.2's Faktura DB writer to walk
    customer.contactId → organizationNumber so we can match the Fiken
    customer to a Notion Fakturamottaker row (by Orgnr).

    Returns the contact dict on 200, None on 404 or any other error.
    Best-effort: a missing contact is logged at INFO and the caller
    leaves the Fakturamottaker relation blank — Notion is fine with
    that, and the operator can hand-link.
    """
    async with await _client() as client:
        try:
            response = await _with_retries(
                lambda: client.get(
                    f"/companies/{company_slug}/contacts/{contact_id}"
                ),
                op_name="get_contact",
            )
        except Exception:  # noqa: BLE001
            return None
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        return None
    return response.json()


async def list_sent_invoices(
    company_slug: str,
    *,
    issue_date_ge: str | None = None,
) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/invoices` — every SENT invoice, paginated.

    Drafts live at the separate /invoices/drafts path and are NOT
    returned here. Presence on this endpoint IS the "sent" signal.

    `issue_date_ge` is optional `YYYY-MM-DD`. When set, the response
    is filtered to invoices issued on or after that date (Fiken's
    `issueDateGe` param). The Phase C.3a manual trigger doesn't
    use this (it scans all sent invoices for the project and lets
    the matching logic decide); the future hourly poller will pass
    a small lookback window. Pagination is 0-indexed (see
    list_contacts docstring).
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": 100}
            if issue_date_ge:
                params["issueDateGe"] = issue_date_ge
            response = await _with_retries(
                lambda p=params: client.get(
                    f"/companies/{company_slug}/invoices",
                    params=p,
                ),
                op_name="list_sent_invoices",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


async def list_credit_notes(
    company_slug: str,
    *,
    issue_date_ge: str | None = None,
) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/creditNotes` — every credit note, paginated.

    Mirrors list_sent_invoices. The poller writes Faktura DB rows
    with Type=Kreditnota for each one. The Phase C.3a manual
    trigger does NOT scan credit notes (a project-scoped trigger
    matches by project relation, and credit notes don't always
    carry a clean reference.our we can match on). They land via
    the future hourly poller (C.3b).
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": 100}
            if issue_date_ge:
                params["issueDateGe"] = issue_date_ge
            response = await _with_retries(
                lambda p=params: client.get(
                    f"/companies/{company_slug}/creditNotes",
                    params=p,
                ),
                op_name="list_credit_notes",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


async def list_products(company_slug: str) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/products` — paginated.

    Mirrors list_contacts. The engine uses this to discover any existing
    per-discipline products by `productNumber` so a fresh deployment
    adopts what's already in Fiken instead of duplicating them. Fiken's
    pagination is 0-indexed (see list_contacts docstring).
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            response = await _with_retries(
                lambda p=page: client.get(
                    f"/companies/{company_slug}/products",
                    params={"page": p, "pageSize": 100},
                ),
                op_name="list_products",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


async def list_bank_accounts(company_slug: str) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/bankAccounts` — paginated.

    Returns every bank account the company has registered in Fiken (each
    carries `bankAccountId`, `bankAccountNumber`, `accountCode`, `name`,
    `type` ("normal" | "tax" | ...), `inactive` bool). The engine reads
    this to auto-pick the first active normal account when sending a
    draft, so the printed invoice shows a real Kontonummer instead of a
    blank field.

    Path is `/bankAccounts` (camelCase) — verified empirically. The
    `/bank-accounts` (kebab-case) form returns an unparseable response.
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            response = await _with_retries(
                lambda p=page: client.get(
                    f"/companies/{company_slug}/bankAccounts",
                    params={"page": p, "pageSize": 100},
                ),
                op_name="list_bank_accounts",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


# ============================================================
# Writes — invoice drafts
# ============================================================


# NOTE: previous versions of this file exported `create_product` /
# `update_product` and a `INCOME_ACCOUNT_SERVICES_VAT = "3020"` constant.
# Both went away when the engine switched to operator-managed Fiken
# products (Kategori name match → productId on the line; account routes
# from the product itself, not from the app). Reintroduce only if a new
# use case truly needs auto-managed products.


async def get_invoice_draft(
    company_slug: str, draft_id: str
) -> dict[str, Any]:
    """`GET /companies/{slug}/invoices/drafts/{id}` — read a single draft.

    Used right after creation to pull the draft's `uuid`, which is what
    Fiken's web UI uses in its URL path (`/foretak/{slug}/fakturautkast/{uuid}`).
    The numeric draftId from the POST response goes nowhere useful in
    the UI.
    """
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get(
                f"/companies/{company_slug}/invoices/drafts/{draft_id}"
            ),
            op_name="get_invoice_draft",
        )
        _raise_for_status(response)
        return response.json()


async def check_draft_exists(
    company_slug: str, draft_id: str
) -> str:
    """`GET /companies/{slug}/invoices/drafts/{id}` — non-raising lookup.

    Returns one of three strings:
      "exists"   — Fiken returned 200, the draft is still unsent.
      "gone"     — Fiken returned 404, the draft was deleted or sent.
                   (When sent, Fiken moves it from /drafts/{id} to
                   /invoices/{newId}; the draft path 404s. Either way,
                   it's no longer an unsent draft.)
      "unknown"  — Network error, 5xx, or other non-2xx/non-404. Caller
                   should fail-safe (NOT cleanup our audit row) to avoid
                   accidentally unblocking a real in-flight draft during
                   a Fiken outage.

    Sibling of `get_invoice_draft` (which raises on non-2xx). Used by
    `_project_has_unsent_draft` to verify our Postgres audit state
    matches Fiken before blocking a Send-faktura click — operators
    can delete drafts in Fiken's UI without telling us, so the
    Postgres row alone can be stale.
    """
    try:
        async with await _client() as client:
            response = await _with_retries(
                lambda: client.get(
                    f"/companies/{company_slug}/invoices/drafts/{draft_id}"
                ),
                op_name="check_draft_exists",
            )
    except Exception:  # noqa: BLE001
        return "unknown"
    if response.status_code == 200:
        return "exists"
    if response.status_code == 404:
        return "gone"
    return "unknown"


async def create_invoice_draft(
    company_slug: str,
    *,
    customer_id: int | None,
    issue_date: str,
    days_until_due_date: int,
    reference: str,
    lines: list[dict[str, Any]],
    invoice_type: str = "invoice",
    currency: str = "NOK",
    your_reference: str | None = None,
    invoice_text: str | None = None,
    bank_account_number: str | None = None,
) -> dict[str, Any]:
    """`POST /companies/{slug}/invoices/drafts` — create a DRAFT invoice.

    Drafts are NOT sent — they sit in Fiken until the user clicks Send
    in the UI.

    Customer linking: pass `customer_id` (Fiken's numeric contactId,
    resolved by caller from the Notion Orgnr → /contacts lookup). If
    None, the field is OMITTED from the body — and Fiken WILL 400
    ("'customerId' er påkrevd"). The engine (sync_fiken_invoice.py)
    never sends None: when Notion has no Orgnr it links to a shared
    "Mangler kunde" placeholder contact instead, so the draft stays
    creatable and the operator picks the real customer in Fiken's UI
    before clicking Send. The None branch here is preserved as a
    defensive escape hatch for ad-hoc callers (debug routes, scripts)
    that explicitly want to surface Fiken's required-field error.

    Required Fiken fields we always send:
      - issueDate (YYYY-MM-DD)
      - daysUntilDueDate (int — payment terms in days from issue)
      - type ("invoice" / "cash" / "creditNote"; we always send "invoice")
      - lines (per-discipline rows from sync_fiken_invoice)
      - REFERENCE_FIELD ("ourReference" — what the Make replacement
        poller matches the project on; we send the Notion project name)

    Optional:
      - `your_reference` ("yourReference") — deres referanse, i.e. the
        customer-side PO / mark on the invoice. Goldbox reads this off
        the Project's `Faktura merkes` rich_text. Omitted from the body
        when blank/None.
      - `invoice_text` ("invoiceText") — the draft-level "Kommentar" in
        Fiken's UI (a free-text note shown above the line items on the
        printed invoice). Goldbox sends a different default for oppstart
        vs slutt invoices (see settings.fiken_invoice_text_*). Omitted
        from the body when blank/None, in which case Fiken falls back to
        the company-level default ("endre standard" in the UI). Empirical:
        verified by direct probe (`PROBE-invoiceText` test) that this
        is the field the Fiken UI reads — alternatives `comment` /
        `message` / `paymentText` are silently dropped on POST.

    `lines` shape (per the new product-linked engine path):
        [{"productId": 1234,            # Fiken numeric id; account auto-fills
          "description": "Næring - Interiør",  # mirrors product name
          "comment": "Kjøkken - Hovedbilde dag",  # smaller sub-line
          "quantity": 0.5,              # Antall (fraction OK)
          "unitPrice": 1000000,         # NOK øre (integer cents) — full Pris
          "vatType": "HIGH",
          "discount": 15.0},            # optional; percent
        ...]

    Free-text lines (no `productId`) require `incomeAccount` — Fiken 400s
    with "incomeAccount is required for free-text lines (lines without a
    productId)" otherwise. The engine reserves free-text only as the
    fallback when a kategori has no matching Fiken product, and treats
    that 400 as a clean surface for the operator to add the missing
    product before re-clicking.

    `bank_account_number` (Fiken field `bankAccountNumber`) populates the
    Kontonummer on the printed invoice. Omitted when blank/None →
    Fiken's UI shows "Kontonummer endre standard" (i.e. blank). Field
    name pinned empirically: `bankAccountCode` and `bankAccountId` are
    silently dropped on POST; only `bankAccountNumber` round-trips.

    Returns the created draft (carries `draftId` for the URL writeback
    onto the Notion project).
    """
    body: dict[str, Any] = {
        "type": invoice_type,
        "issueDate": issue_date,
        "daysUntilDueDate": days_until_due_date,
        "currency": currency,
        REFERENCE_FIELD: reference,
        "lines": lines,
    }
    if customer_id is not None:
        body["customerId"] = customer_id
    if your_reference:
        body["yourReference"] = your_reference
    if invoice_text:
        body["invoiceText"] = invoice_text
    if bank_account_number:
        body["bankAccountNumber"] = bank_account_number

    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/companies/{company_slug}/invoices/drafts", json=body
            ),
            op_name="create_invoice_draft",
        )
        _raise_for_status(response)
        # Fiken's response on POST drafts is sometimes empty with the
        # location of the created draft in the Location header; handle
        # both shapes so the engine always gets a dict.
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        location = response.headers.get("Location") or ""
        return {"Location": location}


async def list_invoice_drafts(company_slug: str) -> list[dict[str, Any]]:
    """`GET /companies/{slug}/invoices/drafts` — every UNSENT draft, paginated.

    The counterpart to `list_sent_invoices` (which lists SENT invoices on
    the /invoices path). Used by the orphan-draft reconciliation tooling:
    a draft present here whose numeric `draftId` has no `FikenInvoice`
    audit row in Postgres is an orphan (created by a run that crashed
    before recording it, or created by hand in Fiken's UI). Pagination is
    0-indexed (see list_contacts docstring).
    """
    rows: list[dict[str, Any]] = []
    page = 0
    async with await _client() as client:
        while True:
            params: dict[str, Any] = {"page": page, "pageSize": 100}
            response = await _with_retries(
                lambda p=params: client.get(
                    f"/companies/{company_slug}/invoices/drafts",
                    params=p,
                ),
                op_name="list_invoice_drafts",
            )
            _raise_for_status(response)
            data = _unwrap_list(response.json())
            if not data:
                break
            rows.extend(data)
            if len(data) < 100:
                break
            page += 1
    return rows


async def delete_invoice_draft(company_slug: str, draft_id: str) -> str:
    """`DELETE /companies/{slug}/invoices/drafts/{id}` — non-raising.

    Returns one of:
      "deleted" — Fiken returned 2xx, the draft is gone.
      "gone"    — Fiken returned 404, the draft was already deleted/sent.
                  (Idempotent: the caller's goal — "this draft no longer
                  exists" — is satisfied either way.)
      "error"   — any other status / network failure. Caller should log
                  and NOT assume the draft was removed.

    Draft deletion was deliberately omitted from this wrapper in v2 (the
    operator did it in Fiken's UI). It's added now for the orphan-draft
    cleanup tooling — the engine can create duplicate drafts when a run
    crashes between the POST and the audit write, and the operator needs
    a programmatic way to clear them.
    """
    try:
        async with await _client() as client:
            response = await _with_retries(
                lambda: client.delete(
                    f"/companies/{company_slug}/invoices/drafts/{draft_id}"
                ),
                op_name="delete_invoice_draft",
            )
    except Exception as err:  # noqa: BLE001
        logger.warning(
            "delete_invoice_draft: %s failed: %s", draft_id, err
        )
        return "error"
    if response.status_code == 404:
        return "gone"
    if 200 <= response.status_code < 300:
        return "deleted"
    logger.warning(
        "delete_invoice_draft: %s returned unexpected status %s",
        draft_id,
        response.status_code,
    )
    return "error"


async def create_credit_note_draft(
    company_slug: str,
    *,
    customer_id: int | None,
    issue_date: str,
    days_until_due_date: int,
    reference: str,
    lines: list[dict[str, Any]],
    currency: str = "NOK",
    your_reference: str | None = None,
    credit_note_text: str | None = None,
    bank_account_number: str | None = None,
) -> dict[str, Any]:
    """`POST /companies/{slug}/creditNotes/drafts` — create a DRAFT
    kreditnota. Mirrors `create_invoice_draft` but POSTs to the
    creditNotes endpoint with `type="credit_note"`.

    Body shape is identical to invoice drafts (Fiken's
    `invoiceishDraftRequest` schema is shared). Empirically pinned
    via probe (2026-06-21): `type` must be the literal string
    `"credit_note"` (underscore, not `creditNote` / `kreditnota` —
    those all 400 with "type er påkrevd").

    Sign convention: send POSITIVE quantities + positive unitPrice
    exactly like an invoice draft. Fiken's UI shows them positive on
    the draft; the sign flip to negative net happens only when the
    user clicks Send (the resulting sent credit note carries
    negative net/gross). So pass amounts in their natural positive
    form — the engine doesn't need to negate anything.

    `credit_note_text` is sent as `invoiceText` (NOT `creditNoteText`).
    Empirically pinned: the kreditnota DRAFT endpoint shares the
    `invoiceishDraftRequest` schema with the invoice draft endpoint
    so it expects `invoiceText`. The `creditNoteText` field only
    appears on SENT kreditnotas (Fiken renames it server-side at Send
    time). Both represent the same Kommentar shown above the line
    items on the printed document.

    Returns the created draft dict (carries `draftId`).
    """
    body: dict[str, Any] = {
        "type": "credit_note",
        "issueDate": issue_date,
        "daysUntilDueDate": days_until_due_date,
        "currency": currency,
        REFERENCE_FIELD: reference,
        "lines": lines,
    }
    if customer_id is not None:
        body["customerId"] = customer_id
    if your_reference:
        body["yourReference"] = your_reference
    if credit_note_text:
        body["invoiceText"] = credit_note_text
    if bank_account_number:
        body["bankAccountNumber"] = bank_account_number

    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/companies/{company_slug}/creditNotes/drafts", json=body
            ),
            op_name="create_credit_note_draft",
        )
        _raise_for_status(response)
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        location = response.headers.get("Location") or ""
        return {"Location": location}


async def get_credit_note_draft(
    company_slug: str, draft_id: str
) -> dict[str, Any]:
    """`GET /companies/{slug}/creditNotes/drafts/{id}` — read a single
    kreditnota draft. Mirrors `get_invoice_draft`: used to fetch the
    draft's `uuid` after creation so we can build the Fiken UI URL
    (which paths on uuid, not numeric draftId)."""
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.get(
                f"/companies/{company_slug}/creditNotes/drafts/{draft_id}"
            ),
            op_name="get_credit_note_draft",
        )
        _raise_for_status(response)
        return response.json()


__all__ = [
    "FIKEN_API_BASE",
    "REFERENCE_FIELD",
    "create_contact",
    "get_invoice_draft",
    "VAT_TYPE_25_PCT",
    "FikenAPIError",
    "whoami",
    "list_companies",
    "list_contacts",
    "list_products",
    "list_bank_accounts",
    "create_invoice_draft",
    "list_invoice_drafts",
    "delete_invoice_draft",
]
