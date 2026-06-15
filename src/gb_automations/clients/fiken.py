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


# ============================================================
# Writes — products + invoice drafts
# ============================================================


# Norwegian chart-of-accounts code for "Salgsinntekt tjenester, høy
# mva-sats" — kontonummer 3020 (sales of services at 25% VAT). Goldbox
# bills architectural visualization services, so every product the
# engine creates books to this account. NOT 3000 (which is "varer" =
# goods). Hardcoded because it's the same forever for a services
# business; if Goldbox ever needs a different code (export-exempt,
# non-VAT, etc.) this becomes a settings variable.
INCOME_ACCOUNT_SERVICES_VAT = "3020"


async def create_product(
    company_slug: str,
    *,
    name: str,
    product_number: str,
    unit_price: float,
    vat_type: str = VAT_TYPE_25_PCT,
    active: bool = True,
    income_account: str = INCOME_ACCOUNT_SERVICES_VAT,
) -> dict[str, Any]:
    """`POST /companies/{slug}/products` — create a product.

    `product_number` is the stable identifier we use to find this product
    again (e.g. `goldbox-interior`). `unit_price` is NOK (decimal). The
    response carries `productId` (Fiken's numeric id); the engine caches it
    in `FikenProductCache` so subsequent runs PUT the price update directly.

    `income_account` is the Norwegian kontonummer the revenue from this
    product books to — Fiken requires it on every product. Defaults to
    3000 (taxable services).
    """
    body = {
        "name": name,
        "productNumber": product_number,
        "unitPrice": unit_price,
        "vatType": vat_type,
        "active": active,
        "incomeAccount": income_account,
    }
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.post(
                f"/companies/{company_slug}/products", json=body
            ),
            op_name="create_product",
        )
        _raise_for_status(response)
        # Fiken returns 201 Created with NO body and the new resource id
        # in the Location header (`/api/v2/companies/{slug}/products/{id}`).
        # Some tenants also return the full JSON. Handle both: try JSON
        # first, fall back to parsing the Location header. Either way the
        # engine ends up with a dict carrying productId.
        try:
            data = response.json()
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
        location = response.headers.get("Location") or ""
        product_id = location.rsplit("/", 1)[-1] if location else ""
        return {
            "productId": product_id,
            "productNumber": product_number,
            "Location": location,
        }


async def update_product(
    company_slug: str,
    product_id: str,
    *,
    name: str,
    unit_price: float,
    vat_type: str = VAT_TYPE_25_PCT,
    income_account: str = INCOME_ACCOUNT_SERVICES_VAT,
    active: bool = True,
) -> dict[str, Any]:
    """`PUT /companies/{slug}/products/{id}` — full update.

    Fiken validates the PUT body against the full product schema —
    `name`, `vatType`, `incomeAccount` are all required on every call,
    not just the fields the caller changed. So this wrapper always
    sends the full set. Caller must pass current values for the fields
    that aren't being changed (the engine reads them from its own
    FikenProductCache or computes the defaults).

    Returns the response (may be empty body + Location header — we
    don't depend on the body shape here, the engine just needs the
    PUT to succeed).
    """
    body: dict[str, Any] = {
        "name": name,
        "unitPrice": unit_price,
        "vatType": vat_type,
        "incomeAccount": income_account,
        "active": active,
    }
    async with await _client() as client:
        response = await _with_retries(
            lambda: client.put(
                f"/companies/{company_slug}/products/{product_id}", json=body
            ),
            op_name="update_product",
        )
        _raise_for_status(response)
        try:
            data = response.json()
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
        return {"productId": product_id, "name": name}


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
) -> dict[str, Any]:
    """`POST /companies/{slug}/invoices/drafts` — create a DRAFT invoice.

    Drafts are NOT sent — they sit in Fiken until the user clicks Send
    in the UI.

    Customer linking: pass `customer_id` (Fiken's numeric contactId,
    resolved by caller from the Notion Orgnr → /contacts lookup). If
    None, the field is OMITTED from the body — Fiken's docs say
    customerId is required, but we've also seen drafts created without
    a customer that the operator fills in by hand in Fiken's UI. If
    Fiken 400s on the missing field, the engine surfaces it and we
    revisit the design.

    Required Fiken fields we always send:
      - issueDate (YYYY-MM-DD)
      - daysUntilDueDate (int — payment terms in days from issue)
      - type ("invoice" / "cash" / "creditNote"; we always send "invoice")
      - lines (per-discipline rows from sync_fiken_invoice)
      - REFERENCE_FIELD ("ourReference" — what the Make replacement
        poller matches the project on)

    `lines` shape:
        [{"description": "Interiør", "quantity": 2.5,
          "unitAmount": 250000,  # NOK øre (integer cents)
          "vatType": "HIGH",
          "productId": 1234}    # optional
        , ...]

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
    "create_product",
    "update_product",
    "create_invoice_draft",
]
