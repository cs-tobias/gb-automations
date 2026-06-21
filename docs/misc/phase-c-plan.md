# Phase C — Fiken sent-invoice poller → Notion (final plan)

Replaces the existing Make.com Fiken automation. When the CEO clicks **Send** on a
Fiken draft, Notion reflects it within ~1 hour: a new row appears in the
**Faktura DB** with the full invoice payload, the Project gains a relation to
that row, and per-Oppgave billing history advances.

This document is the single source of truth for Phase C. It supersedes all
earlier drafts in the conversation.

---

## 1. Detection mechanism — hourly poll

**Why polling, not webhooks.** Fiken's v2 API has no webhook/event/subscription
endpoints. Probed `/webhooks`, `/hooks`, `/subscriptions`, `/events`,
`/notifications`, `/integrations` at both root and per-company scope — all return
404 against live `api.fiken.no` with our working auth. The third-party swagger
mirror (`bjerkio/fiken-js`) has no event paths either. Albato, Make, Apideck —
every integrator in Fiken's ecosystem polls. Fiken is poll-only by design.

**Cadence.** Hourly (`hour="*"`, `minute=7`, Europe/Oslo). Matches the client's
prior Make setup; CEO is already comfortable with this lag. The 02:00 / 03:17 /
03:30 existing jobs (Toggl, Gmail watches, Frame keepalive) are spread on
different minutes so a slow Fiken response can't queue-block them.

**Endpoints polled per company:**

```
GET /api/v2/companies/{slug}/invoices?page=0&pageSize=100&issueDateGe=YYYY-MM-DD&sortBy=lastModified%20desc
GET /api/v2/companies/{slug}/creditNotes?page=0&pageSize=100&issueDateGe=YYYY-MM-DD&sortBy=lastModified%20desc
```

Fiken's `issueDate` is the date the invoice was sent/finalized (drafts don't
have one), so `issueDateGe=today` means "invoices sent today or later." Drafts
live at the separate `/invoices/drafts` path and are NOT polled.

**Cursor strategy.** New `FikenSentInvoiceCursor` table, PK = `company_slug`,
columns `last_issue_date`, `last_polled_at`. Per poll:

1. `issueDateGe = max(cursor - 1 day, today - 7 days)`. The 1-day overlap
   absorbs Fiken's date-only timezone quirk. The 7-day floor caps backfill cost
   on first run / after long outages.
2. Filter responses against `FakturaNotionCache` (records we've already written)
   and process anything new.
3. After processing, advance the cursor to `max(issueDate seen)`.

Idempotent: a repeated poll over the same window is a no-op because cached
records are skipped.

---

## 2. Notion schema changes

### 2a. Projects DB

**Split the single existing `Faktura status` column into two:**

**`Faktura status` (Select) — billing history. Poller-only writes.**

| Option | Meaning |
|---|---|
| `Ikke fakturert` (default) | nothing sent |
| `Fakturert 50%` | oppstart invoice sent; slutt remains |
| `Fakturert` | fully invoiced (slutt sent, or single-shot full bill) |

**`Faktura handling` (Status) — operator intent + draft lifecycle. Writes: operator + `send_faktura` + poller (clears).**

| Option | Set by |
|---|---|
| (blank) | initial; poller clears here after each graduation |
| `Til oppstartsfaktura` | operator |
| `Til avslutningsfaktura` | operator |
| `Utkast laget` | `send_faktura` after draft created |

**`Fakturaer` (relation → Faktura DB, multi) — new property.** Reverse relation
of the Faktura DB's `Prosjekt` property. Notion auto-syncs; the poller writes
the Faktura side.

**`Faktura utkast` (url) — unchanged.** Draft URL written by `send_faktura`;
transient (404s once draft sent). Lives on Projects.

**Faktura merkes** (rich_text) — unchanged, drives `yourReference`.
**Fakturamottaker** (relation), **Fakturert sum** (rollup) — unchanged.

### 2b. Oppgaver DB

**Split the single existing `Fakturert status` column into two:**

**`Fakturert status` (Select) — billing history. Poller-only writes.**

| Option | Meaning |
|---|---|
| `Ikke fakturert` (default) | not billed |
| `Fakturert 50%` | oppstart invoice sent; slutt remains |
| `Fakturert` | fully billed |

**`Faktura handling` (Status) — intent + draft state. Writes: `send_faktura` + poller (clears).**

| Option | Set by |
|---|---|
| (blank) | initial; poller clears after graduation |
| `Utkast 50%` | `send_faktura` (oppstart run) |
| `Utkast 100%` | `send_faktura` (slutt run) |

**`Utgår` stays where it already lives** on Oppgaver — operator-only "don't
bill me" exclusion. (Per session decision: Utgår is its own concept, not part
of the history/intent split. Treated as a separate eligibility gate.)

**Pris, Rabatt, Fakturert beløp, Oppgave kategori, Beskrivelse, Type** —
unchanged. **Navn, Prosjekt, Status (deliverable lifecycle), Runde, Parent item,
Frame.io url, Thumbnail** — unchanged.

### 2c. Faktura DB (existing — operator already has it)

The operator's existing Make-populated DB. Phase C writes new rows here. Engine
NEVER PATCHes existing rows after creation.

| Column | Type | Source on invoice | Source on credit note |
|---|---|---|---|
| Navn | title | `invoiceNumber` | `creditNoteNumber` |
| Number (fakturanummer) | number | `invoiceNumber` parsed int | `creditNoteNumber` parsed int |
| Number (kreditnota til faktura) | number | blank | `associatedInvoiceId` parsed int |
| Number (netto) | number | `net` | `net` |
| Type | select | `Faktura` | `Kreditnota` |
| Kommentar | rich_text | `message` (fallback `comment`) | same |
| Fakturamottaker | relation → Fakturamottaker DB | resolved: customer.contactId → Orgnr → Fakturamottaker row | same |
| Prosjekt | relation → Projects DB | matched: `reference.our == project_title` (casefold + trim) | same |
| Deres_ref | rich_text | `reference.yours` | same |
| Dato | date | `issueDate` | `issueDate` |
| Vår_ref | rich_text | `reference.our` | same |
| Fakturamottaker tekst | rich_text | `customer.name` (denormalized for visibility when relation is collapsed) | same |
| URL | url | `https://fiken.no/foretak/{slug}/faktura/{invoiceId}` | `https://fiken.no/foretak/{slug}/kreditnota/{creditNoteId}` (verify in step §7.2) |

**Relations left blank when unresolvable.** Engine never raises on a no-match;
operator can fix the relation in Notion manually.

---

## 3. Eligibility matrix (`send_faktura` draft creation)

Reads BOTH columns on each Oppgave plus Utgår + Korreksjonsrunde Type as before:

| `Fakturert status` (history) | `Faktura handling` (intent) | oppstart click | slutt click |
|---|---|---|---|
| `Ikke fakturert` | blank | ✅ include | ✅ include |
| `Ikke fakturert` | `Utkast 50%` | ⛔ skip (already on draft) | ✅ include (slutt bills other half) |
| `Ikke fakturert` | `Utkast 100%` | ⛔ skip | ⛔ skip |
| `Fakturert 50%` | blank | ⛔ skip (oppstart done) | ✅ include |
| `Fakturert 50%` | `Utkast 100%` | ⛔ skip | ⛔ skip |
| `Fakturert` | any | ⛔ skip | ⛔ skip |
| any | any (but Utgår=true) | ⛔ skip | ⛔ skip |
| Type=`Korreksjonsrunde` | any | ⛔ skip (admin sub-row) | ⛔ skip |

Bulletproof rules from Phase B still apply:
- Missing Pris → kr 0 line (don't skip)
- Missing Kategori → free-text with `incomeAccount = 3020`
- Renegotiation over-bill on slutt (remaining ≤ 0) → silent skip (surfaced via
  Notion's Budsjett vs Fakturert sum rollups)

---

## 4. Lifecycle writers — who writes what when

### 4a. `send_faktura` (draft creation) writes intent only

- Project `Faktura handling` → `Utkast laget` (was: terminal `Fakturert*` state)
- Each Oppgave on the draft, `Faktura handling`:
  - oppstart run → `Utkast 50%`
  - slutt run → `Utkast 100%`
- Project `Faktura utkast` URL → draft URL (unchanged from today)
- Per-row `Fakturert beløp` → running NOK total (unchanged from today; still
  written on draft creation since the NOK landed on the draft, and Fiken does
  not silently delete drafts)

### 4b. Poller writes history + clears intent

After successfully creating the Faktura DB row for a sent invoice, in order:

1. Read `FikenInvoiceLine` audit rows for `(company_slug, fiken_invoice_id)` to
   know which Oppgaver were on this invoice.
2. For each such Oppgave, set `Fakturert status`:
   - if invoice was oppstart → `Fakturert 50%`
   - if invoice was slutt (or single-shot full bill) → `Fakturert`
3. Clear each such Oppgave's `Faktura handling` to blank.
4. On the Project: if no remaining balance across all Oppgaver → `Faktura status
   = Fakturert`; else `Faktura status = Fakturert 50%`.
5. Clear Project's `Faktura handling` to blank.
6. Faktura DB row already created in step 0 of this sequence (before the status
   writes), so its `Prosjekt` relation auto-fills the Project's `Fakturaer`.
7. Mark `FikenInvoice.sent_at` + `sent_url` + `invoice_number` in Postgres.

**Credit notes.** A `Kreditnota` row in the Faktura DB is created same way,
but the per-Oppgave status flips do NOT fire. Credit notes don't unbill
specific Oppgaver in our model — they're recorded for the operator to see,
not propagated to Oppgave history. (If the future need arises, we add it
then; out of scope for Phase C.)

**Loop-prevention** via existing read-first/skip-if-same in
`set_oppgave_billed` / `set_project_faktura_status` (already shipped). Same
pattern extended to a new `set_oppgave_handling` / `set_project_handling`
clearing helper.

---

## 5. Match strategy: Fiken record → Notion project

**Empirically pinned (§7.1 probe done):** Fiken mints a new `invoiceId` at
send, but the draft's `uuid` is preserved as `invoiceDraftUuid` on the sent
invoice. This is the guaranteed FK.

Three-tier resolution, in order:

1. **Primary: `draft_uuid → invoiceDraftUuid` lookup.** `send_faktura` already
   fetches the draft's `uuid` (used for the draft URL); Phase C adds a
   `draft_uuid` column on `FikenInvoice` and stores it. On poll, the sent
   invoice's `invoiceDraftUuid` is matched against `FikenInvoice.draft_uuid`
   → `project_page_id`. Single SELECT, exact match.

2. **Fallback: `reference.our` match.** Strip + casefold project title, exact
   match against every Project's title in Notion. Catches invoices sent
   manually in Fiken's UI (which won't have an `invoiceDraftUuid` pointing at
   one of our drafts) and pre-Phase-B sent invoices.

3. **No-match.** Log INFO, create the Faktura row with `Prosjekt` relation
   blank, skip status writes. Operator fixes in Notion manually. Visible at
   `/debug/fiken/sent-invoices?status=no_match`.

For credit notes: `reference.our` is primary (credit notes are typically
created in Fiken's UI without a draft predecessor); `associatedInvoiceId` can
walk through to the parent invoice's project as a fallback.

---

## 6. Files to add / modify

### New

- `src/gb_automations/sync/poll_fiken_sent.py` — engine: poll + reconcile + status graduation
- `src/gb_automations/sync/notion_faktura_db.py` — `create_faktura_row` writer + customer→Fakturamottaker resolver
- `migrations/versions/<rev>_phase_c.py` — Alembic: add `FakturaNotionCache`, `FikenSentInvoiceCursor`, three new columns on `fiken_invoices`

### Modified

- `src/gb_automations/clients/fiken.py` — add `list_sent_invoices`, `list_credit_notes`, `get_contact`, `get_sent_invoice_url`, `get_sent_credit_note_url`
- `src/gb_automations/clients/notion.py` — add `set_oppgave_handling`, `set_project_handling`, `clear_oppgave_handling`, `clear_project_handling` (or extend existing helpers with an Optional `None` clear sentinel)
- `src/gb_automations/sync/sync_fiken_invoice.py`:
  - `_eligible_rows`: read both columns, apply matrix from §3
  - post-draft writes: write `Faktura handling` (not `Faktura status`)
  - keep `Fakturert beløp` write as today
- `src/gb_automations/config.py`:
  - new: `FAKTURA_DB_ID`, `PROJECTS_FAKTURAER_PROP = "Fakturaer"`, `PROJECTS_HANDLING_PROP = "Faktura handling"`, `OPPGAVER_PROPS["handling"] = "Faktura handling"`
  - new: `FAKTURA_HANDLING_*` option-string constants (`Til oppstartsfaktura`, `Til avslutningsfaktura`, `Utkast laget`)
  - new: `OPPGAVE_HANDLING_*` option-string constants (`Utkast 50%`, `Utkast 100%`)
  - new: `FAKTURA_PROPS` dict (the 13-column mapping from §2c)
  - new: `FAKTURA_TYPE_FAKTURA`, `FAKTURA_TYPE_KREDITTNOTA`
  - new: `sync_fiken_sent_invoices: bool = True`
  - **delete**: `FAKTURA_STATUS_OPPSTART_DONE` (folded into handling=Utkast laget)
  - **repurpose**: `FAKTURA_STATUS_TIL_OPPSTART` etc. — these are now options on `Faktura handling`, not `Faktura status`. Rename the Python constants to `FAKTURA_HANDLING_TIL_*` for clarity; `FAKTURA_STATUS_TO_INVOICE_TYPE` reads from the handling column too.
- `src/gb_automations/models.py` — add `FakturaNotionCache`, `FikenSentInvoiceCursor`; extend `FikenInvoice` with `sent_at`, `sent_url`, `invoice_number`
- `src/gb_automations/jobs/scheduler.py` — wire hourly job (gated on `settings.sync_fiken_sent_invoices` + `settings.faktura_db_id`)
- `src/gb_automations/sync/queue.py` + `queue_worker.py` — new task type `fiken_sent_poll` with `{company_slug}` payload; dispatch arm calls `poll_company_for_sent_invoices`
- `src/gb_automations/routes/debug.py` — `GET /debug/fiken/sent-invoices`, `POST /debug/fiken/poll-now`

### Docs

- `CLAUDE.md` — Fiken paragraph: add Phase C blurb + the two-column split (history vs handling)
- `docs/misc/fiken-integration.md` — new "Phase C — sent-invoice poll" section: status model, cursor, match strategy, credit notes
- `docs/notion-db-names.md` — Faktura DB section + updated Project/Oppgaver column lists
- `docs/misc/notion-setup.md` — operator one-time setup (see §8)

---

## 7. Verification

### 7.1 Probe: does Fiken mint a new ID at draft→send?

Before the matcher is written. Manually create a draft via `Send faktura`, send
it in Fiken's UI, compare:

```powershell
docker compose exec -T api python -c "import asyncio; from gb_automations.clients import fiken; c=fiken.client(); print(asyncio.run(c.list_drafts('cinesuit-as')))"
# Then inspect the resulting sent invoice — compare ID
```

Outcome shapes the §5 primary match: if IDs match, PK lookup works; if Fiken
re-mints, the `reference.our` fallback becomes the primary path and PK lookup
is just a fast-skip for known sent invoices.

### 7.2 Pin sent-invoice + credit-note URL formats

Click a sent invoice in Fiken UI, copy URL. Same for a credit note. Update
`get_sent_invoice_url` / `get_sent_credit_note_url` to match.

### 7.3 Unit tests

```powershell
.\.venv-test\Scripts\python.exe -m pytest tests/ --tb=short
```

New test files:
- `tests/test_notion_faktura_db.py` — column-by-column write verification, idempotency, no-match → blank relation
- `tests/test_poll_fiken_sent.py` — cursor advance/overlap, match by id, match by reference, no-match logging, oppstart vs slutt status graduation, credit-note path
- `tests/test_sync_fiken_invoice.py` — extend with new eligibility-matrix rows (Utkast 50% + Utkast 100% gates) and post-draft handling writes

### 7.4 Alembic round-trip

```powershell
docker compose exec api alembic upgrade head
docker compose exec api alembic downgrade -1
docker compose exec api alembic upgrade head
```

### 7.5 Smoke test on real data

```powershell
docker compose up -d --build api
curl.exe -X POST http://localhost:8000/debug/fiken/poll-now
docker compose logs -f api | Select-String fiken_sent_poll
curl http://localhost:8000/debug/fiken/sent-invoices?limit=20
```

### 7.6 End-to-end with the CEO

Per the rollout plan (§8):

1. Operator sets up Notion per §8.
2. Operator: create draft via Send faktura. Verify Project shows `Faktura
   handling = Utkast laget`, Oppgaver show `Faktura handling = Utkast 50%`.
   `Faktura status` columns stay at `Ikke fakturert`.
3. CEO: Send in Fiken.
4. Wait up to 1 hour (or `POST /debug/fiken/poll-now` for immediate).
5. Verify:
   - New row in Faktura DB with all 13 columns populated correctly
   - Project `Fakturaer` relation points at the new row
   - Project `Faktura status` flips to `Fakturert 50%`, `Faktura handling`
     clears to blank
   - Each billed Oppgave `Fakturert status` flips to `Fakturert 50%`,
     `Faktura handling` clears
6. Operator sets Project `Faktura handling = Til avslutningsfaktura`, clicks
   Send faktura. Verify intermediate states again.
7. CEO sends second draft. Within 1 hour: Project `Faktura status = Fakturert`,
   all Oppgaver `Fakturert`, all `Faktura handling` blank.
8. Side-by-side compare a Phase-C Faktura row vs. a Make-created Faktura row.
   Adjust formatting (rich_text shape, denormalization) if anything diverges.

---

## 8. Operator-side rollout checklist (one-time)

Before flipping `sync_fiken_sent_invoices = true`:

1. **Notion — Projects DB:**
   - Add `Faktura handling` (Status property) with options blank +
     `Til oppstartsfaktura`, `Til avslutningsfaktura`, `Utkast laget`.
   - Edit existing `Faktura status` (Select) options: keep `Ikke fakturert`,
     `Fakturert 50%`, `Fakturert`; **delete** `Til oppstartsfaktura`,
     `Til avslutningsfaktura`, `Til fakturering`, `Oppstart fakturert`,
     `Utkast laget`.
   - Add `Fakturaer` (relation → Faktura DB, multi).
2. **Notion — Oppgaver DB:**
   - Add `Faktura handling` (Status property) with options blank + `Utkast 50%`,
     `Utkast 100%`.
   - Edit existing `Fakturert status` (Select) options: keep `Ikke fakturert`,
     `Fakturert 50%`, `Fakturert`; remove `Utkast 50%` / `Utkast 100%` if they
     ever got added here. Utgår stays where it already is (per session
     decision — it's its own concept, not handling).
3. **Notion — Faktura DB:**
   - Confirm all 13 columns from §2c exist and match the names exactly.
   - Share with the gb-automations integration.
   - Confirm `Prosjekt` relation points at the Projects DB and the reverse
     property on Projects is `Fakturaer` (rename if Notion auto-named it
     differently).
4. **.env on the running container:**
   - `FAKTURA_DB_ID=<the Faktura DB id>`
   - `SYNC_FIKEN_SENT_INVOICES=true` (default; can leave unset)
5. **Disable** the existing Make.com Fiken automation.
6. **Reload stack:** `docker compose up -d --force-recreate api`.

---

## 9. What is NOT in Phase C

- **Credit notes propagating to Oppgave status.** Faktura DB rows are created;
  Oppgave history is not adjusted. Future work if operators need it.
- **Backfilling pre-Phase-C sent invoices.** Cursor starts at "today - 7 days"
  on first run; older sent invoices stay in Make's existing rows.
- **Two-way edits.** Editing an invoice in Fiken after sending doesn't
  re-PATCH the Faktura row. Out of scope.
- **On-demand per-project poll button.** `/debug/fiken/poll-now` exists for
  ops; per-project button is easy follow-up.
- **Multi-company.** Code is keyed on `company_slug` everywhere; scheduler
  enqueues `settings.fiken_company_slug` only. Trivial expansion when needed.

---

## 10. Risk summary

| Risk | Mitigation |
|---|---|
| Fiken re-mints invoice ID at send | `reference.our` fallback covers it; verify in §7.1 |
| Credit-note URL format unknown | verify in §7.2 before deploy |
| Make-created Faktura rows look different from ours | side-by-side in §7.6; one-line fix-ups expected |
| Poller crashes mid-pass | step-by-step writes; idempotent on next pass (cache + cursor are atomic markers) |
| Notion DB renames break things | engine reads property names from config constants; one-line fix |
| Status option rename in Notion | engine reads option strings from config constants; reader falls back between Select/Status shapes already |
| Fiken auth expires | existing token-refresh path; poll fails loud + retries via queue backoff |
| First poll after long outage backfills too much | 7-day floor on `issueDateGe` |
