# Fiken integration — research + plan

Status: **Phase B v2 shipped** (June 2026) — single-button label-driven invoice creation. Phase C (read-back poller) still **not started**.

This doc captures the original Fiken→Notion research (sections below) so we don't re-discover any of it later; the Phase B engine notes are inline next.

## Phase B v2 — Notion → Fiken invoice creation (live)

**Button**: one "Send faktura" on the Projects DB. The Project's `Faktura status` (Notion `status` property type) holding `Til oppstartsfaktura` / `Til avslutningsfaktura` (or its synonym `Til fakturering`) decides whether the engine bills 50% of every eligible Oppgave or the remainder. After a successful run the engine flips Faktura status to `Oppstart fakturert` / `Fakturert` and the next click is a clean skip.

**Per-Oppgave billing label**: each row's `Fakturert status` (Notion `status` property type) picks whether it's included. Options: `Ikke fakturert` (default, eligible for both modes), `Fakturert 50%` (engine-written after oppstart; slutt eligible), `Fakturert` (engine-written after slutt — skipped on every future run), `Utgår` (operator-only — skipped in every mode).

**Notion is the invoice ledger.** Each Oppgave carries `Pris` (mutable — operators can renegotiate between oppstart and slutt), `Rabatt` (Notion `Percent` format — operator types `15` for 15%, Notion's API returns the fraction `0.15`, engine consumes directly), and `Fakturert beløp` (running total of NOK actually billed, engine-written). The slutt run computes `remaining = Pris × (1 − Rabatt) − Fakturert beløp`, so a renegotiated Pris flows through correctly: oppstart at 15 K Pris bills 7.5 K → operator drops Pris to 10 K → slutt bills 2.5 K (not the original 7.5 K). A project-level rollup of `Fakturert beløp` is the displayed "Fakturert sum." **Rabatt is forwarded to Fiken on the line itself** via the per-line `discount` field (percent), so the printed invoice shows "Pris kr X / Rabatt Y% / Sum kr Z" rather than a pre-discounted unit price. For a slutt run the engine sends the inverted pre-rabatt amount (`unitPrice = to_bill / (1 − Rabatt)`) so Fiken's discount math lands on the same `to_bill` value the audit trail records.

**Invoice fields**:

- `ourReference` (Vår referanse) = Notion project name. Same field the Phase C poller will match on.
- `yourReference` (Deres referanse) = Project `Faktura merkes` rich_text. Optional; omitted from the draft when blank.
- Line text comes in two pieces driven by the Oppgave's `Oppgave kategori` multi_select (first selected label wins):
  - `description` (bold first line) = the **Kategori label** (e.g. `Næring - Interiør`, `Print`). Falls back to `"{Navn} — {Beskrivelse}"` (or just Navn / Beskrivelse / discipline name) when the row has no Kategori.
  - `comment` (smaller sub-line) = `"{Navn} - {Beskrivelse}"` (or just Navn when Beskrivelse is blank). So the customer reads the category up top and the specific deliverable underneath.
- `incomeAccount` per line is also driven by Kategori via `FIKEN_KATEGORI_TO_ACCOUNT` in [config.py](../../src/gb_automations/config.py) — `3020` for tjeneste (`Næring - Interiør`, …), `3000` for vare (`Print`, …). Unknown/blank Kategori → defaults to `3020` with a WARN log. Both still book at 25% MVA (`HIGH`); only the income account differs so the accountant can split tjeneste vs vare revenue.
- Free-text lines (no `productId`) so we control the printed text entirely. The per-discipline product catalogue (`goldbox-interior` / `…-exterior` / `…-animation` / `…-other`) is still mirrored in Fiken so per-product reports stay populated.
- Draft-level "Kommentar" (Fiken API field: `invoiceText`) is set per `invoice_type` from two env-driven defaults:
  - `FIKEN_INVOICE_TEXT_OPPSTART` — printed on oppstart drafts. Default: "Oppstartsfaktura: 50 % av avtalt beløp for oppstart av prosjektet. Resterende beløp faktureres ved levering."
  - `FIKEN_INVOICE_TEXT_SLUTT` — printed on slutt drafts (both `Til avslutningsfaktura` and the synonym `Til fakturering`). Default: "Sluttfaktura: Gjenstående beløp etter eventuell oppstartsfaktura. Takk for at du valgte Goldbox."

  Edit either in `.env` and `docker compose up -d --force-recreate api` to push the change. An empty string in either var → engine omits the field on that mode's drafts and Fiken falls back to the company-level default ("endre standard" in the UI). Empirically verified field name: alternative candidates (`comment`, `message`, `paymentText`) are silently dropped by Fiken on POST.

**Webhook**: `POST /webhooks/notion/send-faktura` (bearer auth via `NOTION_WEBHOOK_SECRET`). Enqueues a `send_faktura` task — never inline. The worker dispatcher routes to [sync/sync_fiken_invoice.py](../../src/gb_automations/sync/sync_fiken_invoice.py) `create_fiken_invoice(project_page_id)`.

**Customer resolution**: Project → Fakturamottaker → `Orgnr` (one hop — the Project has a direct `Fakturamottaker` relation). Matched against `/contacts?customer=true` by digits-only org number. On no match (but Orgnr present) the engine auto-creates a Fiken contact using the Fakturamottaker row's title + Orgnr (operator fills address/email in Fiken later). **If Orgnr is missing entirely** (no Fakturamottaker linked yet, or linked but Orgnr column blank) the engine links the draft to a shared **"Mangler kunde" placeholder contact** (name = `settings.fiken_placeholder_contact_name`; auto-created on first use, cached per `company_slug` in `fiken_placeholder_contacts`). The operator then picks or creates the real customer in Fiken's draft UI before clicking Send. Fiken's API rejects drafts with no `customerId` (`"'customerId' er påkrevd"`) so the placeholder is necessary; it also lets the team scan their Fiken drafts list and immediately see which ones still need a real customer. The earlier two-hop walk through Kunder is gone; Kunder may still exist on the Projects DB for views / contacts but the engine no longer reads it.

**Out of scope for v2** (revisit when needed):

- Cancelling / deleting drafts (operator does it in Fiken UI).
- Auto-send (drafts stay drafts until the user clicks Send in Fiken).
- Reading the draft back into Notion before send (Phase C poller catches it once sent).
- Kategori-based product mapping: the client hasn't finalized the Oppgave kategori options yet — when they do, we'll add a config-driven kategori → Fiken product map.

---

## Phase C — Fiken → Notion read-back (still not started)

The original v1 plan below is the Phase C poller (sent invoices + offers → year-partitioned `Fakturaer YYYY` / `Tilbud YYYY` Notion DBs). The schema landed in [migration v7s8t9u0p1q2](../../migrations/versions/v7s8t9u0p1q2_add_fiken_phase_a.py); the engine still needs writing.

The Phase C cutover replaces the existing Make automation that pulls sent invoices from Fiken into a Notion DB today.

---

## What was decided for v1

| Question | Answer |
|---|---|
| Which Fiken objects | **Sent invoices (fakturaer)** and **offers (tilbud)** only |
| Sync direction | **One-way Fiken → Notion** (no writes back, no Notion-driven invoice creation) |
| Project linking | **Yes** — each row links to a Projects DB page (Fiken `projects` field first, customer-name fallback second) |
| Production access | **Not yet enabled** — requires Goldbox to pay 99 NOK/mo for the Fiken API module |

Dev work targets Tobias's personal Fiken account first (per [memory: dev-on-tobias-accounts-prod-on-goldbox](../../../../.claude/projects/c--Users-tobia-Documents-Code-gb-automations/memory/dev-on-tobias-accounts-prod-on-goldbox.md)); Goldbox token swap happens at final rollout.

---

## Fiken API — what we have to work with

Researched June 2026.

### Auth + base

- **Base URL**: `https://api.fiken.no/api/v2`
- **Swagger UI** (source of truth): https://api.fiken.no/api/v2/docs/
- **Auth**: two options
  - **Personal API token** (Bearer header). Created at *Rediger konto → Sikkerhet → Personlige API-nøkler*. Non-expiring, revocable. **This is what we'll use** — single-tenant Goldbox.
  - OAuth2 (auth-code + refresh tokens). Only needed for multi-tenant apps. Skip for v1.
- **Company scoping**: `GET /companies` lists orgs the token can reach; all subsequent calls scoped via `/companies/{companySlug}/…`.
- **Cost**: API module costs **99 NOK/month**. For >5 production users, must email `api@fiken.no` to be promoted out of dev status.

### Resources (v2)

| Resource | Methods | We need in v1? |
|---|---|---|
| `user` | GET | yes (smoke test) |
| `companies` | GET, POST | yes (bootstrap) |
| `accounts` | GET | no — chart of accounts, future phase |
| `bankAccounts` | GET, POST | no |
| `contacts` | GET, POST, PUT | no — we read `customer.name` off invoices, not the roster |
| `products` | GET, POST | no |
| `journalEntries` | GET, POST | no — future phase |
| **`invoices`** (sent) | GET, POST, PUT | **yes — read only** |
| `creditNotes` | GET, POST | no — surfaced as parent invoice status `Kreditert` |
| **`offers`** | GET, POST | **yes — read only** |
| `purchases` | GET, POST | no — explicitly out of scope v1 |
| `sales` | GET, POST | no |
| `inbox` | GET | no |
| `projects` | GET, POST | read-only as part of project-link resolution |

Listing endpoints support pagination (`page`, `pageSize`, max 100), sorting, filtering. Cursor-driven incremental polling via `sortBy=lastModifiedDate%20asc&fromLastModifiedDate=YYYY-MM-DD` (verify exact param spelling on first live call).

### Webhooks

**Not documented in the official v2 spec** — no `/webhooks` paths, no event-subscription mechanism. Third-party platforms (Albato, WooCommerce plugins) advertise "Fiken webhooks" but those are *the third party's* outbound hooks, not native push events from Fiken.

→ **Design as a scheduled poller.** First integration in this repo to be poll-driven outside of Toggl. Can swap to push later if Fiken adds it.

### Rate limits

- **One concurrent request per token.** Not auto-throttled today, but abusers may be banned. The existing single-worker queue serializes us naturally.
- No documented per-minute/per-day caps.

### Norwegian-specific

- **MVA / VAT**: invoice and journal-entry payloads carry VAT codes ("Sticos mvakoder" — Norwegian standard). Line items reference a `vatType` enum.
- **KID**: Swagger shows a `kid` field on sent invoices. Legacy docs sometimes show `bankAccountKid` nested under `paymentDetails`. **Verify exact field name** on first real invoice payload before relying on it.
- Currency, `organizationNumber`, and Norwegian bank account format are first-class on the relevant objects.

### Useful links

- **Primary**: https://api.fiken.no/api/v2/docs/ (Swagger v2 — source of truth)
- https://hjelp.fiken.no/api (Norwegian help center)
- https://fiken.no/api/doc/ (scenario how-tos)
- https://github.com/bjerkio/fiken-js/blob/main/swagger.json (unofficial swagger.json mirror)

---

## Architecture — fits the existing pattern

Fiken slots in almost exactly like the existing Toggl Phase 2 (`toggl_hours_sync`): no webhooks, scheduled poller, singleton queue task, year-partitioned Notion DBs. Re-use what's already proven.

```
APScheduler (every FIKEN_POLL_INTERVAL_MINUTES)
  → enqueue singleton fiken_poll task   (gb_automations.sync.queue.enqueue_fiken_poll)
    → queue_worker drains
      → sync_fiken.poll_once()
        → fiken.list_invoices(since=cursor) + fiken.list_offers(since=cursor)
        → per record: dedup cache lookup → upsert into Fakturaer YYYY / Tilbud YYYY
        → advance SyncCursor (fiken_invoices_last_modified, fiken_offers_last_modified)
```

---

## v1 implementation plan

### New files

| Path | Role |
|---|---|
| `src/gb_automations/clients/fiken.py` | Async httpx wrapper. `list_invoices(slug, since=)`, `list_offers(...)`, `get_invoice/offer`, `whoami`, `list_companies`. Modeled on `clients/toggl.py`. `FikenAPIError.is_stale_object` covers 404/410. |
| `src/gb_automations/clients/notion_fiken_db.py` | Year-DB router. `get_fakturaer_db_for_year(year)` + `get_tilbud_db_for_year(year)`. Self-heals on stale ids. Mirror of `clients/notion_timer_db.py`. |
| `src/gb_automations/sync/sync_fiken.py` | The engine. `poll_once() → FikenPollResult` (dataclass). Per-record upsert keyed on `(company_slug, fiken_id)`. Modeled on `sync/sync_toggl_hours.py`. |
| `migrations/versions/<rev>_add_fiken.py` | 4 new tables + widen `ck_sync_tasks_task_type` + add `uq_sync_tasks_active_fiken_poll` partial-unique index. |

### Edits to existing files

- **`src/gb_automations/models.py`** — add `FikenInvoice`, `FikenOffer`, `FakturaerDbCache`, `TilbudDbCache`. Add `"fiken_poll"` to `SYNC_TASK_TYPES`. Add `FIKEN_POLL_SINGLETON_KEY = "fiken-poll-singleton"`. Extend `SyncTask.__table_args__` with the active-task partial-unique index (mirror Toggl Hours singleton).
- **`src/gb_automations/config.py`** — settings:
  - `fiken_api_token: str = ""`
  - `fiken_company_slug: str = ""`
  - `fiken_parent_page_id: str = ""` (Notion page under which `Fakturaer YYYY` / `Tilbud YYYY` auto-create)
  - `fiken_poll_interval_minutes: int = 30`
  - `fiken_first_poll_window_days: int = 90` (initial backfill cap)
  - `sync_fiken: bool = False` (feature flag, OFF by default)

  Constants: `FAKTURAER_PROPS`, `TILBUD_PROPS`, `FAKTURA_STATUS_MAP`, `TILBUD_STATUS_MAP`, `build_fakturaer_db_schema(*, projects_db_id)`, `build_tilbud_db_schema(*, projects_db_id)`.

  Extend `_validate_required_settings`: if `sync_fiken=True` then token, slug, parent page id, and `projects_db_id` are required.

- **`src/gb_automations/sync/queue.py`** — add `enqueue_fiken_poll()`, parallel to `enqueue_toggl_hours_sync` (singleton via `on_conflict_do_nothing` on the partial-unique index). Export in `__all__`.
- **`src/gb_automations/jobs/scheduler.py`** — register `_enqueue_fiken_poll_job` on `IntervalTrigger(minutes=settings.fiken_poll_interval_minutes)`, gated on `settings.sync_fiken`.
- **`src/gb_automations/jobs/queue_worker.py`** — add `_process_fiken_poll(claimed, progress)` and a dispatch branch in `_process()`. Same shape as `_process_toggl_hours_sync`.
- **`src/gb_automations/routes/debug.py`** — three endpoints:
  - `GET /debug/fiken` — `whoami()` + `list_companies()` + echo slug used.
  - `POST /debug/fiken/poll` — enqueue + wake worker (same code path as the cron).
  - `GET /debug/fiken/invoice/{id}` — raw payload. **Critical** for verifying KID field name + `projects` field shape on the first live invoice.
- **`docs/notion-db-names.md`** — append Fakturaer YYYY + Tilbud YYYY sections.
- **`CLAUDE.md`** — Fiken section under "Current state" + row in the "Where to look when…" table (once shipped).

### Notion DB design

Operator creates the **parent page only** by hand and sets `FIKEN_PARENT_PAGE_ID`. The engine auto-creates `Fakturaer YYYY` / `Tilbud YYYY` on first sighting of an invoice/offer in that year (same pattern as `E-post YYYY` / `Timer YYYY`).

**Fakturaer YYYY**

| Property | Type | Source |
|---|---|---|
| Navn | title | `"Faktura #<invoiceNumber> — <customer.name>"` |
| Fakturanummer | rich_text | `invoiceNumber` |
| Kunde | rich_text | `customer.name` (NOT a relation in v1 — simpler) |
| Prosjekt | relation → Projects | resolved by `_resolve_project_relation` |
| Fakturadato | date | `issueDate` |
| Forfallsdato | date | `dueDate` |
| Status | select | derived (see status logic below) |
| Beløp inkl. mva | number | `gross` |
| Beløp eks. mva | number | `net` |
| MVA | number | `gross - net` |
| Valuta | rich_text | `currency` (usually NOK; **not converted**) |
| KID | rich_text | exact field name verified on first live invoice |
| Fiken | url | `https://fiken.no/foretak/{slug}/fakturaer/{id}` |
| Fiken ID | rich_text | dedup key (hidden in default view) |

**Tilbud YYYY** — same shape with `Tilbudsnummer`, `Tilbudsdato`, `Gyldig til`, status select `Utkast / Sendt / Akseptert / Avslått / Utløpt`. No MVA breakdown, no KID.

### Sync semantics

- **Cursor**: per-source `SyncCursor` row (`'fiken_invoices_last_modified'`, `'fiken_offers_last_modified'`). Each poll passes `?fromLastModifiedDate=<cursor - 1 day>` to absorb timezone/late-write races. First poll backfills `fiken_first_poll_window_days` (default 90).
- **Per-record idempotency**: `FikenInvoice` cache row stores `last_modified_date`. Unchanged → skip. Changed → PATCH diff. Cache miss → create. Stale Notion page id (`NotionAPIError.is_stale_object`) → evict + recreate.
- **Project linking** (`_resolve_project_relation`):
  1. If Fiken row has `projects: [...]` non-empty → match its name against Projects DB title.
  2. Else fall back to `customer.name` → Companies DB (`Navn` field) → walk back-relation to Projects.
  3. No match → write row with empty `Prosjekt`, increment `skipped_no_project_match`, log INFO. Operator hand-links in Notion; subsequent polls don't overwrite (Notion is truth — engine only PATCHes properties it owns).
- **Invoice status derivation** (no Notion call): `cancelled → Kansellert`, `credit-noted → Kreditert`, `paid → Betalt`, `dueDate < today → Forfalt`, `sent/issued → Sendt`, else `Opprettet`. Unknown Fiken state → log WARN, write raw uppercase (Notion auto-creates the select option).
- **Year-DB routing**: keyed on `issueDate.year`, NOT `lastModifiedDate` (a Dec 31 invoice edited Jan 2 still belongs to last year's DB).
- **Drafts filtered out** (assumption — user said "sent invoices"): only rows with `issueDate is not None` are synced. Confirm with user once a live payload exists.

### Critical files to read before implementing

In order:

1. `src/gb_automations/sync/sync_toggl_hours.py` — closest engine analog (windowed poll, dataclass result, year DB, no webhook).
2. `src/gb_automations/clients/toggl.py` — closest client analog (Bearer token, `_with_retries`, paginated reads).
3. `src/gb_automations/clients/notion_timer_db.py` — year-DB router pattern.
4. `src/gb_automations/sync/queue.py` — `enqueue_toggl_hours_sync` shows the exact singleton enqueue pattern.
5. `src/gb_automations/jobs/queue_worker.py` — `_process_toggl_hours_sync` is the handler template.
6. `src/gb_automations/models.py` — `SyncTask`, `TOGGL_HOURS_SINGLETON_KEY`, partial-unique-index pattern.
7. `src/gb_automations/config.py` — `TIMER_PROPS`, `build_timer_db_schema`, `_validate_required_settings`.
8. The latest two Toggl migrations — exact pattern for CHECK widening + partial-unique index.
9. `docs/misc/gotchas.md` — always before debugging.

### Functions / utilities to reuse (don't reinvent)

- `gb_automations.db.SessionLocal`
- `gb_automations.sync.queue.enqueue_*` pattern with `pg_insert(...).on_conflict_do_nothing(...)`
- `gb_automations.clients.notion._client`, `_raise_for_status`, `_with_retries`, `archive_page`, `NotionAPIError.is_stale_object`
- `gb_automations.obs.log_api_error`, `describe_error`, `request_scope` (already wired in worker)
- `gb_automations.models.SyncCursor` (no new cursor table — that's what it's for)

---

## Verification plan

**Without prod access (today):**
- Unit tests in `tests/test_sync_fiken.py`: `_derive_invoice_status(row)` fixtures cover every documented Fiken state; `_resolve_project_relation` with mocked Notion covers projects-field-present / projects-empty / customer-fallback / no-match paths.
- `docker compose up -d --build` with `SYNC_FIKEN=false` (default) — migrations apply, no startup errors. `SYNC_FIKEN=true` without token → `_validate_required_settings` rejects with a clear message.

**With a personal Fiken account (Tobias):**
- Set `FIKEN_API_TOKEN`, `FIKEN_COMPANY_SLUG`, `FIKEN_PARENT_PAGE_ID`, `SYNC_FIKEN=true`. `docker compose up -d --force-recreate api`.
- `Invoke-RestMethod http://localhost:8000/debug/fiken` → auth chain + company list.
- Create a test invoice + offer in personal Fiken.
- `Invoke-RestMethod -Method Post http://localhost:8000/debug/fiken/poll`. Tail `docker compose logs -f api | grep -v /health`. Expect `fiken poll done: created=2 ...`.
- Verify rows in Notion. Edit the invoice's due date in Fiken, re-poll, confirm PATCH (not duplicate).
- `GET /debug/fiken/invoice/<id>` — **inspect raw payload to lock in KID field name and `projects` field shape before they bite.**

**With Goldbox prod (once 99 NOK module enabled):**
- Swap to prod token + slug. First poll backfills last 90 days. Subsequent polls incremental.

---

## Out of scope for v1 (do NOT slip in)

- Purchases / kjøp (received invoices/expenses).
- Contacts/customers roster sync (we read `customer.name` only).
- Credit notes as separate Fiken entities (surfaced as parent invoice status = Kreditert).
- Two-way: no PATCH back to Fiken, no Notion buttons that create invoices.
- Per-line-item rows (invoice's `lines` array).
- Invoice PDFs into Notion (the `Fiken` URL column is the link).
- Sync Queue Notion mirror entries for `fiken_poll` (singleton, no per-row mirror).

---

## Open questions (resolve during impl, not now)

1. **KID field name** — Swagger says `kid`; legacy docs sometimes show `bankAccountKid` nested under `paymentDetails`. Verify via `/debug/fiken/invoice/<id>` on a real invoice; drop the column if absent.
2. **`projects` field usage** — depends on whether Goldbox actively uses Fiken's project module. If always `[]`, the customer-name fallback IS the primary path.
3. **Drafts** — we filter to `issueDate is not None`. Confirm with user once we see live data; trivial to relax.
4. **Pagination** — verify Link headers vs. `?page=N&pageSize=100` on first live call; defensive code handles either.
