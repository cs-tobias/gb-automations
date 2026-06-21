# Fiken integration — research + plan

Status: **Phase B v2 shipped** (June 2026) — single-button label-driven invoice creation. **Phase C.2 shipped** (June 2026) — Faktura DB writer + idempotency cache (dormant until `FAKTURA_DB_ID` is set). **Phase C.3a shipped** (June 2026) — manual graduation trigger (`POST /debug/fiken/graduate-project`) for one project at a time. Phase C.3b (hourly poller running C.3a's engine in a per-project loop) replaces the existing Make automation; C.4 is the prod rollout. (C.1 history/handling split was tried then reverted June 2026 — the draft URL + Postgres audit trail already convey the same info.)

This doc captures the original Fiken→Notion research (sections below) so we don't re-discover any of it later; the Phase B engine notes are inline next.

## Phase B v2 — Notion → Fiken invoice creation (live)

**Button**: one "Send faktura" on the Projects DB. The Project's `Faktura status` (Notion `status` property type) holding `Til oppstartsfaktura` / `Til avslutningsfaktura` (or its synonym `Til fakturering`) decides whether the engine bills 50% of every eligible Oppgave or the remainder. `send_faktura` does NOT write to ANY Notion billing field after a successful draft — neither `Faktura status`, `Fakturert status`, nor `Fakturert beløp`. The operator-picked status stays until the graduation step (Phase C.3) writes the terminal `Fakturert 50%` / `Fakturert` AND adds the audit-line NOK to `Fakturert beløp` once the draft has been SENT in Fiken. Between the operator's click and the graduation, the draft-in-flight signal is the `Faktura utkast` URL on the project plus an unsent `FikenInvoice` row in Postgres (sent_at IS NULL).

**Per-mode block** (NOT per-project): clicking Send faktura while a same-mode unsent draft exists is blocked. An unsent oppstart draft blocks new oppstart clicks but DOES NOT block a slutt click — and vice versa. The slutt math separately treats unsent drafts as committed-but-not-yet-invoiced (subtracts the drafted NOK from `remaining` in `_row_billable_nok`), so a slutt click while an oppstart draft is unsent correctly bills only the leftover. This is operationally important: the CEO can sit on an oppstart draft for weeks; we shouldn't permanently block slutt for a different project phase. The block also self-heals — every check verifies each candidate draft against Fiken's `/invoices/drafts/{id}` endpoint; rows whose drafts are "gone" (operator deleted them in Fiken's UI) get marked sent_at and cleared from the in-flight set automatically.

**Notion is the truth for actually-invoiced money.** Both `Fakturert status` and `Fakturert beløp` change only at graduation time. The Faktura DB row's own `Netto` column shows per-invoice NOK; a project-level rollup of `Fakturert beløp` shows the total actually sent. Drafts are commitments-but-not-money: tracked in Postgres (FikenInvoiceLine), used by the slutt math to avoid double-billing, but never count toward Notion's user-facing numbers until the draft becomes a real sent invoice.

**Per-Oppgave billing label**: each row's `Fakturert status` (Notion `status` property type) picks whether it's included. Options: `Ikke fakturert` (default, eligible for both modes), `Fakturert 50%` (graduation-written after oppstart SENT; slutt eligible), `Fakturert` (graduation-written after slutt SENT — skipped on every future run), `Utgår` (operator-only — skipped in every mode). `send_faktura` does NOT write here at draft creation; only the graduation step writes the `Fakturert*` values.

**Bulletproof eligibility** (the CEO's invariant — "only `Utgår` should keep a row off the draft"): every Oppgave under the project lands as a line on the draft unless it falls under one of these narrow exceptions, ALL of which mean "the row has nothing to add to this run":

1. `Fakturert status = Utgår` — operator explicitly excluded.
2. `Fakturert status = Fakturert` — already fully billed, no remaining money.
3. Oppstart click on a row already at `Fakturert 50%` — already oppstartet, prevents double-bill.
4. Row is currently on a SAME-MODE unsent Fiken draft (FikenInvoiceLine join to a FikenInvoice with `sent_at IS NULL` AND `invoice_type` matching this click) — would duplicate the line on a fresh same-mode draft. Normally the per-mode block check prevents this; defensive belt-and-suspenders. A row on an unsent OPPSTART draft is still eligible for slutt (and vice versa) — the slutt math subtracts the drafted NOK so it bills only the leftover.
5. `Type = Korreksjonsrunde` — auto-generated admin sub-rows from the Frame.io integration; never invoiced.
6. Slutt run where the operator dropped `Pris` below the already-billed `Fakturert beløp` — the row is genuinely over-billed; Notion's `Budsjett` vs `Fakturert sum` rollups surface the discrepancy at project level, and a credit-note flow is a future feature, not something this engine should improvise inline.

Everything else lands, including:

- Rows with non-discipline `Type` (e.g. `Klargjøre modell`) or blank `Type`. Previously these were silently dropped — that was the production bug the CEO hit. Now they land.
- Rows with no `Pris` set (or `Pris = 0`). The engine sends a kr 0 unit price; operator sees the line on the draft and can fix `Pris` in Notion + re-click, or edit the line directly in Fiken.
- Rows with no `Oppgave kategori` (or a Kategori that doesn't match any Fiken product). The engine sends a free-text line with `incomeAccount = FIKEN_FREE_TEXT_INCOME_ACCOUNT` (3020 — services @ 25% VAT) so Fiken accepts the draft. Operator can change the account on the line in Fiken's UI, or add a matching product to Fiken so future drafts go through the product-linked path.

**Notion is the invoice ledger.** Each Oppgave carries `Pris` (mutable — operators can renegotiate between oppstart and slutt), `Rabatt` (Notion `Percent` format — operator types `15` for 15%, Notion's API returns the fraction `0.15`, engine consumes directly), and `Fakturert beløp` (running total of NOK actually billed, engine-written). The slutt run computes `remaining = Pris × (1 − Rabatt) − Fakturert beløp`, so a renegotiated Pris flows through correctly: oppstart at 15 K Pris bills 7.5 K → operator drops Pris to 10 K → slutt bills 2.5 K (not the original 7.5 K). A project-level rollup of `Fakturert beløp` is the displayed "Fakturert sum." **Rabatt is forwarded to Fiken on the line itself** via the per-line `discount` field (percent), so the printed invoice shows "Pris kr X / Rabatt Y% / Sum kr Z" rather than a pre-discounted unit price. For a slutt run the engine sends the inverted pre-rabatt amount (`unitPrice = to_bill / (1 − Rabatt)`) so Fiken's discount math lands on the same `to_bill` value the audit trail records.

**Invoice fields**:

- `ourReference` (Vår referanse) = Notion project name. Same field the Phase C poller will match on.
- `yourReference` (Deres referanse) = Project `Faktura merkes` rich_text. Optional; omitted from the draft when blank.
- **Pris is always Pris.** The engine sends `unitPrice = Pris` (full agreed price, in øre) on every Fiken line, regardless of mode. What changes between oppstart and slutt is Fiken's `quantity` (`Antall` in the UI): `0.5` on oppstart, `remaining/gross` on slutt — so 1.0 for a fresh slutt with no oppstart, 0.5 after a clean oppstart, an arbitrary fraction when Pris was renegotiated mid-flight. Fiken's line math (`quantity × unitPrice × (1 − discount%)`) lands on the same NOK we record as `Fakturert beløp`.
- **Line text + product link driven by Kategori (name match against Fiken's product catalogue):** the FIRST selected `Oppgave kategori` label is looked up in Fiken's products by exact name. On match the engine sends `productId` on the line and Fiken auto-fills BOTH the printed product name AND the `incomeAccount` (3000 / 3020 / etc.) from the product itself — the engine never sends a hardcoded account or product number.
  - `description` (bold first line) = the Kategori label. Mirrors the product name so the operator sees a consistent line whether they're looking at Fiken or the engine's logs.
  - `comment` (smaller sub-line) = `"{Navn} - {Beskrivelse}"` (or just Navn when Beskrivelse is blank). The customer reads the category up top and the specific deliverable underneath.
  - The kategori → productId mapping is cached per-(`company_slug`, `kategori_label`) in `fiken_product_by_kategori`. Cache populates lazily on the first send_faktura that references each kategori.
  - **No-match fallback**: when no Fiken product has the Kategori's exact name, the engine sends a free-text line with `description = Kategori label`. Fiken rejects free-text lines with no `incomeAccount` (HTTP 400 `incomeAccount is required for free-text lines (lines without a productId)`) — that's the operator's prompt to add the missing product in Fiken's UI and re-click. Deliberately not silently choosing an account on the operator's behalf.
- Operators manage the Fiken product catalogue (name, account, vatType, active/inactive) entirely in Fiken's UI. The engine never creates, updates, or renames Fiken products.
- Draft-level "Kommentar" (Fiken API field: `invoiceText`) is set per `invoice_type` from two env-driven defaults:
  - `FIKEN_INVOICE_TEXT_OPPSTART` — printed on oppstart drafts. Default: "Oppstartsfaktura: 50 % av avtalt beløp for oppstart av prosjektet. Resterende beløp faktureres ved levering."
  - `FIKEN_INVOICE_TEXT_SLUTT` — printed on slutt drafts (both `Til avslutningsfaktura` and the synonym `Til fakturering`). Default: "Sluttfaktura: Gjenstående beløp etter eventuell oppstartsfaktura. Takk for at du valgte Goldbox."

  Edit either in `.env` and `docker compose up -d --force-recreate api` to push the change. An empty string in either var → engine omits the field on that mode's drafts and Fiken falls back to the company-level default ("endre standard" in the UI). Empirically verified field name: alternative candidates (`comment`, `message`, `paymentText`) are silently dropped by Fiken on POST.
- Kontonummer (Fiken API field: `bankAccountNumber`) is auto-resolved on the first send_faktura per company by listing the Fiken bank accounts and picking the FIRST active normal-type one. The chosen `bankAccountNumber` is cached in `fiken_bank_accounts(company_slug)`. Operators can override via `FIKEN_BANK_ACCOUNT_NUMBER` in `.env` (string, same format Fiken's UI shows — digits only, no spaces, e.g. `36061538997`); when set, the cache is bypassed and the env value flows through. Empty default + nothing in `.env` → engine auto-picks. Field name pinned empirically: `bankAccountCode` and `bankAccountId` are silently dropped on POST; only `bankAccountNumber` round-trips.

**Webhook**: `POST /webhooks/notion/send-faktura` (bearer auth via `NOTION_WEBHOOK_SECRET`). Enqueues a `send_faktura` task — never inline. The worker dispatcher routes to [sync/sync_fiken_invoice.py](../../src/gb_automations/sync/sync_fiken_invoice.py) `create_fiken_invoice(project_page_id)`.

**"Sjekk fiken" button** (manual graduation fallback): a second Notion button on Projects, identical webhook shape: `POST /webhooks/notion/graduate-faktura` (same bearer auth). Enqueues a `graduate_faktura` task; the worker dispatches to [sync/graduate_project.py](../../src/gb_automations/sync/graduate_project.py) `graduate_project(project_page_id)`. Use cases: testing the integration end-to-end without waiting for C.3b's hourly poller; the CEO just sent an invoice in Fiken and wants it in Notion immediately; forcing a re-check when something looks stale. Same engine the hourly poller will use, on demand.

**Customer resolution**: Project → Fakturamottaker → `Orgnr` (one hop — the Project has a direct `Fakturamottaker` relation). Matched against `/contacts?customer=true` by digits-only org number. On no match (but Orgnr present) the engine **first looks up the Orgnr in Brreg** (the Norwegian Business Registry — open REST at `data.brreg.no/enhetsregisteret/api`, no auth) to get the official legal name, then auto-creates the Fiken contact with that name + Orgnr. **The Brreg name is also written back to Notion**: if the Fakturamottaker row's title differs from Brreg, the engine PATCHes it (one-time enrichment, idempotent on subsequent runs). Operator fills address/email in Fiken later.

**Brreg-by-name fallback** when Orgnr is missing entirely: if the Project's `Fakturamottaker` relation is empty / has a blank Orgnr but the Project has a `Kunder` relation with a title (e.g. `Entur`), the engine searches Brreg by name and applies a strict suffix-aware match — accept only when exactly one result's `navn` equals the query OR `query + " " + suffix` for `suffix ∈ {AS, ASA, ANS, DA, ENK, SA, BA, NUF, AL, BBL, BL, KS, SE, FKF}`. So `Entur` cleanly matches `ENTUR AS` (not `ENTURA HOLDING AS` or `ENTUR LANDSFORENING`). On a clean win, the engine recovers the Orgnr, creates a new Fakturamottaker row in `FAKTURAMOTTAKER_DB_ID` with the Brreg name + Orgnr, links it to the Project, and continues through the normal Orgnr path. Subsequent clicks go through the clean Orgnr path with no Brreg call. Set `FAKTURAMOTTAKER_DB_ID` in `.env` to enable this writeback path; without it, the engine logs and falls through.

**If everything misses** (no Orgnr, no Kunder, or Brreg-by-name returns 0 or 2+ clean matches) the engine links the draft to a shared **"Mangler kunde" placeholder contact** (name = `settings.fiken_placeholder_contact_name`; auto-created on first use, cached per `company_slug` in `fiken_placeholder_contacts`). The operator picks or creates the real customer in Fiken's draft UI before clicking Send. Fiken's API rejects drafts with no `customerId` (`"'customerId' er påkrevd"`) so the placeholder is necessary; it also lets the team scan their Fiken drafts list and immediately see which ones still need a real customer.

Brreg is best-effort at every step (timeout, 5xx, no clean match) — every degraded path falls back to the pre-Brreg behavior. Brreg never blocks a draft. The earlier Project → Kunder → Fakturamottaker walk for the orgnr-required path is gone (Kunder is read only by the Brreg-by-name fallback now); operators set Fakturamottaker directly on the Project for normal billing.

**Out of scope for v2** (revisit when needed):

- Cancelling / deleting drafts (operator does it in Fiken UI).
- Auto-send (drafts stay drafts until the user clicks Send in Fiken).
- Reading the draft back into Notion before send (Phase C poller catches it once sent).
- Bulk product creation: the engine never creates or updates Fiken products — operators manage the catalogue (name, account, VAT, active state) in Fiken's UI. Updating prices in Fiken doesn't break the cache because the engine sends Pris from Notion as `unitPrice` on every draft.

---

## Phase C — operator-side Notion setup (one-time)

Required before the operator sees Phase C.2 + C.3a behavior on prod. The status columns are unchanged from Phase B (the C.1 split was reverted June 2026 — see CLAUDE.md → Fiken section for the why). Only the Faktura DB plumbing is new.

### Projects DB

The existing `Faktura status` property (Status type) is reused as-is. Confirm it has these options:

- `Ikke fakturert` (default)
- `Til oppstartsfaktura`
- `Til avslutningsfaktura` (and/or `Til fakturering` as a synonym)
- `Fakturert 50%`
- `Fakturert`

Delete any leftover options from the C.1 experiment (`Utkast laget`, etc.) — they're no longer used by the engine. Add a new property `Fakturaer` (relation → Faktura DB) so each project shows its sent invoices (Phase C.2 writes the Faktura DB side; the relation back to Projects auto-syncs).

### Oppgaver DB

The existing `Fakturert status` property (Status type) is reused as-is. Confirm it has these four options; delete anything else (especially any `Utkast 50%` / `Utkast 100%` from the C.1 experiment):

- `Ikke fakturert` (default)
- `Fakturert 50%`
- `Fakturert`
- `Utgår`

The engine no longer needs (or reads) a separate `Faktura handling` column on Oppgaver. If you added one during C.1, you can delete it.

### What changes for the operator

- **Workflow is unchanged from Phase B.** Set `Faktura status = Til oppstartsfaktura` (or `Til avslutningsfaktura`), click Send faktura. The Project status stays at the operator-picked value until graduation; the draft URL on `Faktura utkast` is the "draft exists" signal.
- **Post-Send-faktura**: `Faktura utkast` URL appears on the project. Click → opens the draft in Fiken. CEO reviews + clicks Send in Fiken. Status columns DON'T change at this point.
- **After CEO sends in Fiken**: trigger `POST /debug/fiken/graduate-project?project_page_id=<id>` (or wait for C.3b's hourly poller once it ships). The graduation step writes `Fakturert 50%` / `Fakturert` to both the Project and the linked Oppgaver, creates the Faktura DB row, and clears the `Faktura utkast` URL (since the draft is gone in Fiken now).
- **Per-mode block**. Clicking Send faktura while a SAME-MODE draft (oppstart or slutt) is still unsent in Fiken returns a "skipped — unsent `<mode>` draft exists" message. Send or delete the existing draft in Fiken first. A DIFFERENT mode is allowed: if oppstart is sitting unsent, you can still click Send faktura for slutt — it'll create the slutt draft and bill only the remainder (the unsent oppstart's NOK gets subtracted from the per-Oppgave math). Block self-heals: drafts you delete in Fiken's UI are noticed on the next Send-faktura click and cleared automatically.

---

## Phase C — Fiken → Notion read-back (in progress)

Replaces the existing Make.com Fiken automation. Single persistent `Faktura` DB the operator points the engine at via `FAKTURA_DB_ID` in `.env`. Phase C is being shipped in four sub-phases so each one is independently testable + revertible.

### Phase C status

- ~~C.1 — history vs handling split.~~ Tried then reverted June 2026. The Faktura utkast URL + Postgres audit trail already convey "draft in flight"; the extra column was UI clutter for state we already exposed elsewhere.
- **C.2 — Faktura DB writer. Shipped June 2026.** Code that creates rows in the Faktura DB from a Fiken invoice or credit-note payload. Idempotent via Postgres cache. Exercised by `POST /debug/fiken/write-faktura-row` for manual column-by-column verification against Make's existing rows. Engine is **dormant by default** (no writes happen) until `FAKTURA_DB_ID` is set in `.env`. Details: [Phase C.2 section](#phase-c2--faktura-db-writer-details).
- **C.3a — manual graduation trigger. Shipped June 2026.** Per-project, on-demand reconciliation: scan Fiken for sent invoices that belong to this project, match each via draft_uuid FK or reference.our fallback, call C.2's writer to land Faktura DB rows, write terminal `Fakturert 50%` / `Fakturert` to the Project + linked Oppgaver, clear the `Faktura utkast` URL. Exercised by `POST /debug/fiken/graduate-project?project_page_id=<id>`. Returns a structured JSON summary. Idempotent (per-row `sent_at` marker on FikenInvoice + the C.2 cache). Details: [Phase C.3a section](#phase-c3a--manual-graduation-trigger-details).
- **C.3b — hourly poller. Not started.** Runs C.3a's engine in a per-project loop driven by APScheduler. Replaces the existing Make automation. Almost zero new logic — wraps the existing C.3a engine.
- **C.4 — rollout. Not started.** Operator-side prod Notion schema changes, `.env` updates, disable Make.

### Phase C.2 — Faktura DB writer details

**Engine**: [sync/notion_faktura_db.py](../../src/gb_automations/sync/notion_faktura_db.py) `create_faktura_row(company_slug, fiken_record, record_type, project_page_id=None, project_title=None)`. Returns the new Notion page id on success, the cached page id on a re-call for the same Fiken record, or None when the engine is dormant / a Fiken id is missing.

**Idempotency**: every successful write inserts into `faktura_notion_cache(company_slug, record_type, fiken_record_id)` Postgres table (migration `b3c4d5e6f7g8`). The cache is checked BEFORE attempting a write, so re-running for the same Fiken record is a fast no-op. Operators can delete a row in the Notion Faktura DB intentionally (e.g. to correct something) — the cache silently leaves it alone on subsequent polls. Notion is truth.

**Column mapping**: see [docs/notion-db-names.md — Faktura DB section](../notion-db-names.md). The writer fills every column in `FAKTURA_PROPS`; pure helpers (`_extract_record_id`, `_record_number_str`, `_record_number_int`, `_build_url`, `_build_payload`) are unit-tested standalone. `net` is converted from øre → NOK (Fiken returns øre; Notion stores display NOK).

**Customer → Fakturamottaker relation**: `customer.contactId → Fiken /contacts/{id} → organizationNumber → FAKTURAMOTTAKER_PROPS["orgnr"]` lookup against the Fakturamottaker DB. Best-effort: any miss (no FAKTURAMOTTAKER_DB_ID, no contactId, no orgnr, query failure, no match) leaves the relation blank — operator fixes in Notion manually. The customer name is always denormalized into the `Fakturamottaker tekst` rich_text column regardless, so the row is legible without hopping the relation.

**Project → Prosjekt relation**: pre-resolved by the caller (in C.3 this is `reference.our → project_title` exact match). C.2's API takes an optional `project_page_id` + `project_title` so the writer is decoupled from the matching logic. Passing None for both leaves both relations blank — useful for the debug endpoint to test formatting without committing to a project link.

**Debug endpoint**: `POST /debug/fiken/write-faktura-row?fiken_invoice_id=<id>&record_type=<faktura|kreditnota>&project_page_id=<optional>`. Fetches the live Fiken record + walks the writer. Returns the new page id or `engine_disabled: true` when `FAKTURA_DB_ID` is unset. Used for E2E sanity-checking the column-by-column formatting against Make's existing rows before C.3 ships.

**What this module does NOT do** (out of scope):

- Poll Fiken. C.3.
- Update lifecycle statuses on Project / Oppgaver. C.3.
- Re-PATCH existing Faktura rows after creation. Sent invoices are immutable in practice; corrections create new credit-note rows.
- Match what Make wrote previously. Make can keep running during the C.2 → C.4 migration; both writers coexist (they create separate rows for separate records, deduped by the FakturaNotionCache PK).

### Phase C.3a — manual graduation trigger details

**Engine**: [sync/graduate_project.py](../../src/gb_automations/sync/graduate_project.py) `graduate_project(project_page_id)`. Operator-triggered (via debug endpoint); the future C.3b hourly poller will call this same engine in a per-project loop.

**Flow** (one project per call):

1. Read the Notion project page to get its title (used by the fallback match strategy).
2. List every SENT invoice in Fiken via `fiken.list_sent_invoices(company_slug)`. Drafts live at a separate endpoint and are NOT returned.
3. Load this project's FikenInvoice audit rows from Postgres. Build a `{draft_uuid → audit_row}` index.
4. For each sent invoice:
    - Skip if already graduated (FikenInvoice.sent_at NOT NULL).
    - **PRIMARY match**: invoice's `invoiceDraftUuid` equals an audit row's `draft_uuid`. Set strategy = `draft_uuid`. We know exactly which project + which Oppgaver were on this invoice (via FikenInvoiceLine).
    - **FALLBACK match**: invoice's `reference.our` (or top-level `ourReference` on older payloads) casefold-exact-matches this project's title (strip + casefold). Set strategy = `reference.our`. Faktura row lands but status writes skip (no audit trail = can't safely identify Oppgaver).
    - **No-match**: silent skip; increment counter.
5. Per matched invoice (`_reconcile_one`):
    - Call `notion_faktura_db.create_faktura_row` to land the Faktura DB row (idempotent — C.2 cache).
    - On draft_uuid match, call `_graduate_statuses`:
        - Read FikenInvoiceLine rows for this `(company_slug, fiken_invoice_id)`.
        - For each linked Oppgave: write `Fakturert status` = `Fakturert 50%` (oppstart) / `Fakturert` (slutt).
        - On the project: write `Faktura status` to the same terminal value, and clear the `Faktura utkast` URL (the draft URL goes 404 once the draft is sent in Fiken; the Faktura DB row's own URL takes over).
    - On draft_uuid match + no errors, UPSERT `sent_at` + `sent_url` + `invoice_number` on the FikenInvoice row so re-runs short-circuit.

**Hourly graduation poller (C.3b)**: when `SYNC_FIKEN_GRADUATIONS=true` in `.env`, an APScheduler cron fires every hour at minute :07 ([jobs/scheduler.py](../../src/gb_automations/jobs/scheduler.py) `_enqueue_fiken_graduations_for_all_active_projects`). It queries the Projects DB, filters to non-terminal `Faktura status` values (anything that's NOT blank / `Ikke fakturert` / `Fakturert` / `Kreditert`), and enqueues a `graduate_faktura` task per active project. Per-project dedup (`uq_sync_tasks_active_graduate_faktura`) collapses with any in-flight operator Sjekk fiken click. Replaces the operator's need to manually click — CEO sends an invoice in Fiken → within the hour, Notion auto-graduates. Each cron fire logs a one-line summary: `⏰ fiken_graduations cron: scanned=N enqueued=K skipped_terminal=T skipped_dedup=D errors=E`. Gated on the feature flag so a deploy without Fiken doesn't fire a job that just skips itself.

**Til kreditering button mode**: a fourth `Faktura status` option, `Til kreditering`. When the operator sets it + clicks Send faktura, the engine flips into "credit mode": looks up every sent FikenInvoice for this project that doesn't already have a kreditnota, and creates ONE draft kreditnota per parent in Fiken (`POST /creditNotes/drafts`, `type=credit_note`). Lines are mirrored from the parent invoice (positive numbers — Fiken negates on Send). Customer, references, and Kommentar copy from the parent. Per-mode block check: an unsent kreditnota draft blocks new kreditering clicks but does NOT block oppstart/slutt. Operator opens each draft in Fiken, reviews, clicks Send. Then Sjekk fiken pulls the resulting credit notes back into Notion (which already-shipped handles graduation: subtracts NOK from Fakturert beløp, flips Oppgaver to `Kreditert`).

**Kreditnota handling**: Sjekk fiken also lists `/creditNotes` and lands each in the Faktura DB as `Type=Kreditnota`. Match strategies for kreditnotas:

1. `associatedInvoiceId → FikenInvoice.fiken_invoice_id` (primary, exact). When the kreditnota's parent invoice is one we already graduated, the kreditnota inherits the project.
2. `yourReference` casefold-exact against project title (rare manual-only fallback).
3. `creditNoteText` casefold-exact against project title (anchor for CEO-made standalone kreditnotas).

When a kreditnota is matched via the primary path AND its absolute net equals its parent's net (full reversal, tolerated to 1 øre), the engine:

- Subtracts the per-line NOK from each Oppgave's `Fakturert beløp` (clamps to 0)
- Flips each Oppgave's `Fakturert status` to `Kreditert`

Partial credits (net < parent net) intentionally leave statuses alone — too ambiguous to auto-reverse. Operator handles manually.

**Project `Faktura status = Kreditert` is NOT auto-set** in C.3a.y. The correct "all sent fakturas covered by kreditnotas" check needs per-project closure tracking we don't currently store (FakturaNotionCache is keyed on Fiken record id, not project). Operator sets Project status to `Kreditert` manually once they've verified in the Faktura DB that every faktura has a matching kreditnota. Future refinement: add project linkage to the cache.

**Operator-side schema additions** for kreditnota support: add `Kreditert` as an option on both `Faktura status` (Project) and `Fakturert status` (Oppgave) status properties in Notion. Without these, the writes will 400 with "invalid status option".

**Match strategy precedence (and why)**:

1. `draft_uuid → invoiceDraftUuid` — the only EXACT FK. Fiken mints a fresh `invoiceId` at send-time, but the draft's `uuid` is preserved as `invoiceDraftUuid` on the sent record. Zero ambiguity. Only fires for invoices we originated via Send faktura (since that's when our `FikenInvoice.draft_uuid` is populated).
2. `reference.our` (Vår referanse) — casefold-exact against project title. Catches manual invoices when the CEO disciplines themselves to type the project name in the Vår referanse field.
3. `invoiceText` (Kommentar) — casefold-exact against project title. The operational anchor for our actual data: the CEO writes meaningful Kommentar text on every invoice (e.g. "Leie av kamerautstyr til Mills Majo") even when Vår referanse is blank. Name the Notion project the same string as the Kommentar and Sjekk fiken pulls it. Workflow tip: this is how to test the integration against your existing historical invoices — create a Notion project titled exactly the Kommentar and click Sjekk fiken.

If all three miss, the invoice is silently skipped (counted as `skipped_no_match` in the response). Either name the project to match one of the three fields, or hand-graduate via `POST /debug/fiken/graduate-one-invoice`.

**Why status writes skip on the fallback paths** (`reference.our` and `invoice_text`): both fallbacks tell us WHICH project to link to, but NOT which Oppgaver were on the invoice. The CEO created the invoice in Fiken's UI directly; we have no FikenInvoiceLine audit trail. Flipping all the project's Oppgaver to `Fakturert 50%` would be wrong (only some of them were billed). Flipping none is honest. The Faktura DB row still lands so the audit trail is complete; the operator manually flips Oppgave statuses to match reality.

**Trigger**: `POST /debug/fiken/graduate-project?project_page_id=<id>`. Returns the structured summary:

```json
{
  "project_page_id": "...",
  "project_title": "0001_Test",
  "company_slug": "goldbox-as",
  "fiken_invoices_scanned": 5,
  "skipped_already_graduated": 2,
  "skipped_no_match": 2,
  "error": null,
  "matched": [
    {
      "fiken_invoice_id": "4493258455",
      "invoice_number": "10051",
      "issue_date": "2026-06-18",
      "net_nok": 6240.0,
      "match_strategy": "draft_uuid",
      "faktura_db_page_id": "<notion-page-id>",
      "faktura_db_cached": false,
      "oppgave_statuses_set": 3,
      "project_status_set": true,
      "error": null
    }
  ]
}
```

**Idempotency**: re-running the trigger after a successful graduation is a no-op. The Faktura DB cache (C.2) short-circuits writes; `sent_at` on FikenInvoice short-circuits status flips. Safe to call repeatedly.

**What the trigger does NOT do**: scan credit notes (C.3b will), backfill historical pre-C.1 invoices in bulk (operator runs the trigger per project), match invoices that have neither `invoiceDraftUuid` nor `reference.our` set (no path back to a project; operator either fixes the invoice in Fiken or writes the Faktura row manually).

**Testing endpoint (dev-only)**: `POST /debug/fiken/graduate-one-invoice?fiken_invoice_id=<id>&project_page_id=<id>&invoice_type=<oppstart|slutt>&mark_sent_in_db=<bool>` exercises the full graduation flow against ONE historical real invoice from your Fiken, targeted at a chosen Notion project. Bypasses the auto-match logic (covered by unit tests) and proves the downstream code paths — Faktura DB write, Project status flip, draft URL clear, Oppgave status flips when audit lines exist, idempotency on re-run — against unmodified real Fiken payloads. Read-only against Fiken; every write is to Notion + Postgres. Lets us validate the engine without creating + sending new test invoices (which would create real Norwegian ledger entries that need to be credited). Historical invoices won't have `FikenInvoiceLine` audit rows so the Oppgave-status path stays untested by this endpoint — it'll naturally exercise the first time a real draft→send→graduate cycle runs in prod.

### Legacy notes

- `FIKEN_PARENT_PAGE_ID` is **gone** from settings + startup validation. Do not put it in `.env`. The year-partitioned `Fakturaer YYYY` / `Tilbud YYYY` shape was abandoned in favor of one persistent DB; the `FakturaerDbCache` / `TilbudDbCache` tables from [migration v7s8t9u0p1q2](../../migrations/versions/v7s8t9u0p1q2_add_fiken_phase_a.py) are vestigial — never written to, harmless.

---

> ⚠️ **Heads-up**: everything below is the original Phase C v1 research from June 2026. The **year-partitioned `Fakturaer YYYY` / `Tilbud YYYY`** shape it describes is **abandoned**; see the "Phase C" section above for the current direction (single `Faktura` DB via `FAKTURA_DB_ID`). Mentions of `FIKEN_PARENT_PAGE_ID` are stale — that setting no longer exists. Useful as background for the Fiken API surface area; ignore the schema/setup specifics.

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
