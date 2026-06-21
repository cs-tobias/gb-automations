# gb-automations — orientation

Long-term goal: **Notion is the single source of truth** for everything that happens at Goldbox (a Norwegian architectural-visualization studio). This repo is the always-on backend that pulls data from every tool the team uses into Notion, and keeps it bidirectionally in sync where it makes sense. Once the context is in Notion, AI can reason over the whole business.

Full client vision in [docs/reference/client-brief.md](docs/reference/client-brief.md) (Norwegian) — read it for the *why* behind any integration.

## Current state (May 2026)

**Gmail ↔ Notion sync is live and in production.** Stages 1–4c of the README roadmap are done, plus a durable work queue on top.

What works end-to-end:
- New row in Notion `Project` DB → Gmail label created in every seeded user's mailbox (and renamed when the project is renamed in Notion).
- Email labeled with a project label in Gmail → Pub/Sub push → the webhook **enqueues a durable `sync_tasks` row** (it does NOT sync inline) → a background **queue worker** processes it: cleaned body, extracted history (split into per-message rows), participants upserted to `Contacts`/`Companies`, attachments uploaded to Drive and linked, multi-select tags by a local Ollama LLM (taxonomy in [config.py](src/gb_automations/config.py) `EMAIL_TAGS`), written to a year-partitioned `Emails` DB.
- **The queue is the guarantee:** a labeled thread will reach Notion — crash-safe, retry-with-backoff, terminal failures parked as `failed` (visible at `/debug/queue`, never silently lost). Per-project status shows as an icon on the Projects DB; stale Notion ids self-heal. See [docs/misc/gotchas.md](docs/misc/gotchas.md) entry 16.

## Frame.io — Phases 1, 2, and 2.5 all shipped (May 2026)

**DB shape (restructured May 2026)**: ONE **Oppgaver DB** holds deliverables (image/render rows), general internal tasks, AND `Korreksjonsrunde N` sub-rows — distinguished by the `Type` select alone, not by which DB they're in. A real discipline (`Interiør`/`Eksteriør`/`Animasjon`/`Annet`) = deliverable; `Klargjøre modell` (or any non-discipline Type) = internal task (no Frame/NAS). A separate **Korreksjoner DB** holds the individual feedback items (one row per Frame comment), each related to its Korreksjonsrunde row over in Oppgaver. Settings: `OPPGAVER_DB_ID` (deliverables/tasks), `KORREKSJONER_DB_ID` (feedback items). DB/property reference: [docs/notion-db-names.md](docs/notion-db-names.md).

**Phase 1**: Notion → Frame mirror. The per-row "Sync" button (on deliverable rows) enqueues `frame_leveranse_sync` + NAS folder sync (when `SYNC_FRAME=true`) — but ONLY for deliverables (`Type` is a recognized discipline); the webhook gate skips internal rows (e.g. `Type=Klargjøre modell`) and blank-Type rows. Each Notion project becomes its own **top-level Frame Project** under `FRAME_WORKSPACE_ID`; the V00 placeholder lands DIRECTLY under the Project's discipline subfolder (flattened — no per-deliverable wrapping folder). The V00 placeholder image is **dynamically rendered per-deliverable** (no longer a static logo): Frame's `create_file_from_url` fetches `GET /assets/placeholder/{deliverable_page_id}.png` ([routes/assets.py](src/gb_automations/routes/assets.py)), which composes the deliverable's `Beskrivelse` text (falling back to the row title) over its uploaded `Thumbnail` image (or a black canvas) via Pillow ([sync/placeholder_image.py](src/gb_automations/sync/placeholder_image.py)). Frame URLs are written back to the deliverable rows. Renames mirror in place (the placeholder file is renamed too); self-heals on Frame deletions; adopts pre-existing same-name entities. Engine: [sync/sync_frame.py](src/gb_automations/sync/sync_frame.py).

**Phase 2**: Frame comments → Notion. The first comment of round N lazily creates a `Korreksjonsrunde N` sub-row under the deliverable (Oppgaver DB); each comment then becomes a `Korreksjon` row in the Korreksjoner DB, related to that round. Replies nest under the parent comment's Korreksjon (3-level, via Parent item). Engine: [sync/sync_frame_comments.py](src/gb_automations/sync/sync_frame_comments.py). Cache: `FrameComment` table, keyed on the Frame comment UUID, persists `oppgave_page_id` (= the Korreksjon row id) for two-way lookups.

**Phase 2.5**: Status loop + 2-way `Ferdig` ↔ `completed` sync.

- New Frame webhook event `file.versioned` (resource = `version_stack_id`) → engine [sync/sync_frame_version.py](src/gb_automations/sync/sync_frame_version.py) flips the deliverable's `Status` to **Ferdig**.
- New Notion automation on the **Korreksjoner DB** (`Ferdig` checked/unchecked) → `POST /webhooks/notion/oppgave-done` (bearer auth, reuses `NOTION_WEBHOOK_SECRET`) → engine [sync/sync_oppgave_done.py](src/gb_automations/sync/sync_oppgave_done.py) PATCHes the linked Frame comment's `completed` field.
- Frame's existing `comment.completed` / `comment.uncompleted` webhooks → existing comment engine propagates to Notion's `Ferdig` checkbox.
- Either direction triggers a rollup recheck via [sync/sync_leveranse_status.py](src/gb_automations/sync/sync_leveranse_status.py) which sets `Under arbeid` (some done) or `Oppgaver ferdig` (all done).
- **Loop-prevention** via read-first / skip-if-same in `notion_client.set_deliverable_status` and `notion_client.set_row_done` — one round-trip per click, no ping-pong.
- **Manual override**: setting Status to `Trenger avklaring` or `Utgår` suppresses all auto-writes for that deliverable.

Setup: [docs/misc/frame-setup.md](docs/misc/frame-setup.md) (Phase 2.5 section).

Next up after Frame: Toggl (daily aggregated hours → Notion), Fiken (accounting), meeting transcripts, then MCP server + RAG. Also follow-on Frame work: AI-drafted reply suggestions, `Project marked finished in Notion → set inactive in Frame`.

## Fiken — Phase B v2 shipped (June 2026); Phase C.2 + C.3a shipped (June 2026)

**Phase C history note.** An earlier C.1 split this column into two (`Faktura status` history + `Faktura handling` intent + draft-in-flight) to make every state explicit. Reverted June 2026 — the `Faktura utkast` URL on the project + the `Fakturaer` relation already convey "draft in flight" / "we've sent X," so the split was UI clutter for state the rest of the system already exposes. Current model: ONE `Faktura status` (Project) / `Fakturert status` (Oppgave) status property per DB, same options as Phase B. The operator picks `Til oppstartsfaktura` / `Til avslutningsfaktura` to queue a click; the engine writes the terminal `Fakturert 50%` / `Fakturert` values only AFTER the draft has been graduated to a sent invoice (via C.3a / C.3b). At draft creation time `send_faktura` writes NOTHING to either status column.

**Draft-in-flight signal: the URL + Postgres, not Notion status.** When `send_faktura` succeeds it writes the draft URL to `Faktura utkast` on the project (transient — goes 404 once the draft is sent in Fiken; cleared by the graduation step). The eligibility check + per-project block check both read from Postgres: a `FikenInvoice` row with `sent_at IS NULL` for this project means "draft in flight." One unsent draft per project at a time — if `send_faktura` finds one, it BLOCKS the new click with a clear error message ("Send or delete the existing draft in Fiken first"). The eligibility filter additionally takes a `rows_on_unsent_drafts: set[str]` kwarg derived from `FikenInvoiceLine` joins, so a row already on an unsent draft also skips defensively.

**Phase C.2 — Faktura DB writer (shipped, dormant).** [sync/notion_faktura_db.py](src/gb_automations/sync/notion_faktura_db.py) `create_faktura_row(company_slug, fiken_record, record_type, project_page_id, project_title)` creates rows in the operator's Faktura DB from a Fiken invoice or credit-note payload. Idempotent via `faktura_notion_cache(company_slug, record_type, fiken_record_id)` Postgres table; a repeat call for the same Fiken record returns the cached page id. `Customer → Fakturamottaker` relation is resolved by walking `customer.contactId → Fiken /contacts/{id} → organizationNumber → FAKTURAMOTTAKER_PROPS["orgnr"]` (best-effort — blank on any miss). `Prosjekt` relation is pre-resolved by the caller (C.3 poller matches by `reference.our → project_title`). Customer name is denormalized into `Fakturamottaker tekst` rich_text regardless of relation resolution. Engine is **dormant by default**: `settings.faktura_db_id` empty → INFO log + return None. Manual exercise via `POST /debug/fiken/write-faktura-row?fiken_invoice_id=<id>&record_type=<faktura|kreditnota>` — fetches the live Fiken record and walks the writer end-to-end so the operator can sanity-check column-by-column formatting against Make's existing rows before C.3 wires the poller. Column mapping documented in [docs/notion-db-names.md](docs/notion-db-names.md) Faktura DB section; full writer notes in [docs/misc/fiken-integration.md](docs/misc/fiken-integration.md) Phase C.2 section.

**Phase C.3a — manual graduation trigger (shipped).** [sync/graduate_project.py](src/gb_automations/sync/graduate_project.py) `graduate_project(project_page_id)` is the trigger-driven variant of the future hourly poller (C.3b). Same engine, scoped to one project: lists every sent invoice on the Fiken side via `list_sent_invoices`, matches each one via TWO strategies in order — (1) PRIMARY: `draft_uuid → invoiceDraftUuid` exact FK against the FikenInvoice audit row stored by `send_faktura` (Fiken mints a fresh `invoiceId` at send-time, but the draft `uuid` is preserved as `invoiceDraftUuid` on the sent record); (2) FALLBACK: `reference.our` casefold-exact-match against the project title (catches invoices the CEO created directly in Fiken's UI). For each match: creates a Faktura DB row (via C.2's writer, idempotent), and when matched via draft_uuid (i.e. we have a per-line FikenInvoiceLine audit trail), graduates statuses — sets each linked Oppgave's `Fakturert status` to `Fakturert 50%` (oppstart) or `Fakturert` (slutt), sets Project's `Faktura status` likewise, AND clears the `Faktura utkast` URL on the project (the draft URL goes 404 once sent in Fiken; the Faktura DB row's own URL takes over as "here is the invoice"). Marks `FikenInvoice.sent_at` + `sent_url` + `invoice_number` so re-runs are no-ops. Fallback-matched invoices land in the Faktura DB but don't touch statuses (no audit trail = no safe way to know which Oppgaver were on the invoice). Triggered via `POST /debug/fiken/graduate-project?project_page_id=<id>` — returns a structured JSON summary of every Fiken invoice scanned, how each matched, and what changed in Notion. C.3b reuses this engine in a per-project loop driven by APScheduler; almost zero new code at that point.

**Single "Send faktura" button** on the Projects DB drives Notion → Fiken draft creation. **Bulletproof eligibility**: every Oppgave under the project lands on the draft unless it's explicitly excluded. Skip rules: (1) `Fakturert status = Utgår` (operator excluded), (2) `Fakturert status = Fakturert` (already fully billed), (3) oppstart click + `Fakturert status = Fakturert 50%` (oppstart already sent), (4) oppstart click + `Faktura handling ∈ {Utkast 50%, Utkast 100%}` (row is on an unsent draft — re-billing would duplicate), (5) slutt click + `Faktura handling = Utkast 100%` (slutt draft already exists awaiting Send — slutt on `Utkast 50%` is FINE, bills the other half), (6) `Type = Korreksjonsrunde` (Frame.io admin sub-row), or (7) slutt on a row where Pris was renegotiated below Fakturert beløp (over-billed — surfaced via Notion's `Budsjett` vs `Fakturert sum` rollups; credit-note flow is a future feature). Everything else lands — non-discipline Type, missing Pris (kr 0 line), missing Kategori (free-text line with default `incomeAccount = 3020`). The Project's `Faktura handling` (Notion `status` property type) picks the mode (`Til oppstartsfaktura` → bill 50% of every eligible Oppgave; `Til avslutningsfaktura` → bill the remainder; falls back to `Faktura status` during C.1 → C.3 migration). The reader (`notion_client.read_select_name`) falls back between Notion's `select` and `status` shapes so flipping a column type doesn't break reads; writes go through `set_oppgave_billed` (history + NOK total), `set_oppgave_handling` / `set_project_handling` (intent column, supports value=None to clear), and `set_project_faktura_status` (history, poller-only after C.1).

**Notion is the invoice ledger.** Each Oppgave carries `Pris` (mutable — operators can renegotiate between oppstart and slutt), `Rabatt` (Notion `Percent` format — type `15` for 15%; API returns fraction `0.15`, engine consumes directly so do NOT divide by 100 again), and `Fakturert beløp` (engine-written running NOK total). Slutt computes `remaining = Pris × (1 − Rabatt) − Fakturert beløp`, so a renegotiated Pris flows through correctly. **Pris is always Pris** — the engine sends `unitPrice = Pris` on every Fiken line every run; Fiken's `Antall` (quantity) carries the fraction (0.5 oppstart, `remaining/gross` slutt). Rabatt is forwarded as the per-line `discount` percent. Fiken's `Antall × Pris × (1 − discount%)` lands on the same NOK we stamp into `Fakturert beløp`. A project-level rollup of `Fakturert beløp` is the displayed `Fakturert sum`. Vår referanse = project title; Deres referanse = project `Faktura merkes` (rich_text). Line product link: the FIRST `Oppgave kategori` multi_select label is matched (by exact name) against Fiken's operator-managed product catalogue; on hit the engine sends `productId` on the line (Fiken auto-fills product name + `incomeAccount` from the product itself — the engine NEVER sends a hardcoded account). Cached per-(company_slug, kategori_label) in `fiken_product_by_kategori`. The line also carries `description = Kategori label` (mirrors the product name) and `comment = "Navn - Beskrivelse"` (per-row context). No matching product → free-text fallback; Fiken 400s on missing incomeAccount, prompting the operator to add the missing product in Fiken. Customer resolved via one direct relation: Project → `Fakturamottaker` → Orgnr, with `/contacts` auto-creation on no-match when Orgnr is present (so a project can bill a different entity than the parent Kunder uses by default). Missing Orgnr (no Fakturamottaker linked, or Orgnr blank) is NOT a failure. **Brreg-by-name fallback first**: if the Project has a `Kunder` relation with a name, the engine searches Brreg's open REST (`data.brreg.no`) and accepts only a strict suffix-aware exact match (e.g. `Entur` → `ENTUR AS` only, never `ENTURA HOLDING AS`). On a clean win, the engine recovers the Orgnr, creates a new row in `FAKTURAMOTTAKER_DB_ID` with the Brreg name + Orgnr, links it to the Project, and proceeds through the normal Orgnr path. **When Orgnr IS present**, the engine looks it up in Brreg before the Fiken auto-create and uses the official `navn` — and PATCHes the Notion Fakturamottaker title to match (one-time, idempotent enrichment). **If nothing resolves** (no Orgnr, no Kunder, or Brreg search returns 0 or 2+ clean matches), engine links the draft to a shared "Mangler kunde" placeholder contact (auto-created on first use, cached in `fiken_placeholder_contacts` per `company_slug`; name configurable via `settings.fiken_placeholder_contact_name`). Fiken's API rejects drafts with no `customerId`, so the placeholder is required; operator picks/creates the real customer in Fiken's draft UI before clicking Send. Brreg is best-effort at every step — never blocks a draft. Most recent draft URL lands in a single `Faktura utkast` column on the Project — transient by design (the URL points at a Fiken DRAFT specifically; once the draft is sent it becomes a real invoice on a different URL and this one 404s, which is the correct "graduated" signal). Kontonummer on the printed invoice (`bankAccountNumber`) is auto-resolved on first send_faktura per company by picking the first active normal-type account from Fiken's `/bankAccounts`; cached in `fiken_bank_accounts`. Operator can override via `FIKEN_BANK_ACCOUNT_NUMBER` in `.env`. Setup: [docs/misc/fiken-integration.md](docs/misc/fiken-integration.md) (Phase B v2 section).

Phase C is being shipped in sub-phases: ~~C.1 (history/handling split — reverted, see history note above)~~, **C.2 (Faktura DB writer + idempotency cache) — shipped June 2026 (dormant until FAKTURA_DB_ID is set)**, **C.3a (manual graduation trigger) — shipped June 2026 (POST /debug/fiken/graduate-project for one project at a time)**, C.3b (hourly Fiken sent-invoice poller + per-project loop over C.3a's engine) replaces the existing Make automation, C.4 is the prod rollout. Operator schema changes are listed in [docs/notion-db-names.md](docs/notion-db-names.md) and [docs/misc/fiken-integration.md](docs/misc/fiken-integration.md). Make automation stays running until C.3b ships + C.4 disables it.

## Architecture in one diagram

```
                   ┌─────────────────────────────────┐
                   │  FastAPI (src/gb_automations)   │
                   │  ───────────────────────────    │
   Notion ────────►│  /webhooks/notion               │
   webhook         │     → ENQUEUE label_sync task   │   (worker creates/renames
                   │       (does NOT sync inline)    │    Gmail label + NAS folder)
                   │  /webhooks/notion/resync-thread │
                   │     → enqueue a thread rebuild  │
                   │                                 │
   Gmail Pub/Sub ─►│  /webhooks/gmail                │
   push            │     → ENQUEUE sync_tasks row    │   (does NOT sync inline)
                   │       (+ cursor advance, 1 txn) │
                   │                                 │
                   │  Queue worker (lifespan task)   │
                   │     → claims pending task       │
                   │     → sync_thread() → Notion    │   (retry/backoff,
                   │     → done | failed             │    1 at a time)
                   │                                 │
                   │  APScheduler → renew watches/5d │
                   └──────────┬──────────────────────┘
                              │
                ┌─────────────┼─────────────┐
                ▼             ▼             ▼
            Postgres     Ollama        secrets/
            (sync_tasks  (tagging       gcp-service-
             QUEUE +      LLM)          account.json
             dedup cache,
             cursors)
```

The Gmail side is a **durable queue**: the webhook only enqueues (crash-safe), and a single background worker drains it. A labeled thread *will* reach Notion — retries on failure, parks terminal failures as `failed`, never silently lost. See [docs/misc/gotchas.md](docs/misc/gotchas.md) entry 16 for the full queue semantics, and `/debug/queue` for live state.

Everything runs in Docker Compose. Public traffic enters via Cloudflare Tunnel at `https://hub.<domain>/…`. Fresh deployments follow the short checklist in [docs/guide.md](docs/guide.md), which fans out to [docs/misc/google-setup.md](docs/misc/google-setup.md), [docs/misc/notion-setup.md](docs/misc/notion-setup.md), and [docs/misc/cloudflare-setup.md](docs/misc/cloudflare-setup.md). The GCP side is one paste of [scripts/gcp-bootstrap.sh](scripts/gcp-bootstrap.sh) into Cloud Shell. Painful lessons captured in [docs/misc/gotchas.md](docs/misc/gotchas.md) — **always check gotchas.md before debugging an integration issue**, most have an entry.

> Ignore `docs/misc/setup.md`, `docs/misc/setup-manual.md`, and `src/gb_automations/scripts/setup_workspace.py` (+ `_installer/`). The auto-installer was abandoned; the docs above are the real flow. Don't reference, link, or modify those files unless explicitly asked.

## Repo layout

```
src/gb_automations/
  main.py             FastAPI app, logging config, lifespan
  config.py           pydantic-settings, EMAILS_PROPS, EMAIL_TAGS taxonomy
  db.py               async SQLAlchemy engine + SessionLocal
  models.py           SyncCursor, User, EmailRow, ContactCache, CompanyCache, ProjectLabel,
                      ProjectFolder, EmailsDbCache, SyncTask (the durable queue), …
  obs.py              request_scope() + ColorFormatter (colored logs) + log_api_error/describe_error
  routes/
    webhooks.py       /webhooks/{echo,notion,gmail} + /webhooks/notion/resync-thread.
                      gmail webhook ENQUEUES (doesn't sync inline)
    debug.py          /debug/{databases,notion,llm,queue} + POST /debug/queue/retry-failed
  clients/
    gmail.py          DWD-impersonated Gmail wrapper (sync); list_history paginates
    notion.py         Notion REST wrapper (async httpx); NotionAPIError.is_stale_object, page_is_live
    notion_emails_db.py year-partitioned Emails DB resolver — self-heals a stale cached db id
    drive.py          Drive uploads for email attachments (sync)
    llm.py            Ollama tagging — loads prompt from prompts/*.md
  sync/
    sync_thread.py    THE engine — Gmail thread → Notion rows. Called by the queue
                      worker (not the webhook). Idempotent; self-heals stale cache ids
    queue.py          Durable queue DB API: enqueue/claim/mark/retry/reset, status rollups
    queue_worker.py   Long-lived lifespan task — drains the queue, retry/backoff, dot updates
    queue_mirror.py   Best-effort Notion mirrors: "Sync Queue" DB + Projects-DB status icon
    resync_project.py rebuild_thread (archive+recreate one thread), enqueue_missing (boot reconcile)
    backfill_project_labels.py  rebuild the ProjectLabel cache from Notion + Gmail
    watches.py        Gmail users.watch() lifecycle
  utils/
    email_cleaning.py   strip signatures, quoted history, etc.
    email_splitting.py  split forwarded chains into individual messages
    history_extraction.py  regex-based "On X wrote:" detection (replaced LLM splitter)
    participants.py   parse From/To/Cc, internal-vs-external classification
    phone.py          extract NO/intl phone numbers from signatures
  jobs/
    scheduler.py      APScheduler — renews Gmail watches
    queue_worker.py   (the worker module; imported into main.py lifespan)
  scripts/            one-shot CLIs (seed_users, start_watches, pull_llm_model, reset_thread,
                      backfill_project_labels, reconcile, retry_failed, resync_project, sync_one)
prompts/
  default.md          generic tagging prompt
  goldbox.md          Goldbox-specific tagging prompt (set TAGGING_PROMPT_PATH)
docs/
  guide.md            short checklist for a fresh deployment (THE entrypoint)
  notion-db-names.md  the Notion DB/property names used in this workspace
  misc/
    google-setup.md     GCP / Workspace steps (paired with scripts/gcp-bootstrap.sh)
    notion-setup.md     Notion integration, DB setup, Sync-to-Gmail button
    cloudflare-setup.md Cloudflare zone + tunnel setup
    nas-setup.md        Office NAS (W: drive) mount + toggles
    gotchas.md          16 entries of "this cost me hours, here's the fix" (incl. queue semantics)
    setup.md, setup-manual.md   ABANDONED auto-installer docs — ignore
  reference/          client brief, original Apps Script, prior Claude design chats, real logs
migrations/           Alembic (sync engine for migrations; app uses async). Auto-applied on
                      container start (Dockerfile CMD runs `alembic upgrade head`)
tests/                pytest — unit tests for cleaning/splitting/extraction/participants/queue
```

## Key conventions

- **Python ≥3.12, async FastAPI + async SQLAlchemy**. Sync Google client calls go through `asyncio.to_thread` / a thread pool — don't block the event loop.
- **Dedup belongs in Postgres, truth in Notion.** `EmailRow` / `ContactCache` / `CompanyCache` / `EmailsDbCache` are local caches to avoid re-querying Notion; we still write to Notion on every change. Because Notion is truth, a cached id can go stale (object deleted/archived) — the read paths **self-heal**: on a 404 / "archived ancestor" error they evict the cache row and re-resolve. Don't add a new cached-id read without that fallback.
- **Sync work is queued, not inline — for ALL webhooks, including buttons.** The Gmail webhook enqueues a `thread` task; the "Sync to Gmail" Projects button enqueues a `label_sync` task (the worker dispatches on `SyncTask.task_type`). The worker ([jobs/queue_worker.py](src/gb_automations/jobs/queue_worker.py)) runs `sync_thread` / `sync_project_labels`. Both must stay idempotent (re-running is a no-op / repair). New work belongs in/after the worker, reached via the queue — **never block a webhook (especially a Notion button) on external API work**: it'll exceed Notion's button timeout (gotchas §17). New "do something per email" work goes in/after `sync_thread`.
- **Notion `Emails` DB property names live in `EMAILS_PROPS`** in `config.py`. Same for `CONTACTS_PROPS`. If a property is renamed in Notion, edit the constant — don't hard-code names elsewhere.
- **Settings are env-driven**, validated at startup (`_validate_required_settings`). New required env vars get a startup check, not a runtime crash.
- **Logging**: `logger = logging.getLogger(__name__)`. INFO is visible by default (see `main.py` dictConfig). Use the `request_id` filter — already wired — so every line during a webhook carries `[notion:abcd]` or `[gmail:abcd]`.
- **Build the end-state, not stepping stones.** See `~/.claude/projects/.../memory/build-end-state-no-stepping-stones.md`. When adding a new integration (e.g. Frame), pick the right shape on day 1 — don't build a CLI-only proof first if the end state is webhook-driven.
- **No comments explaining *what*** — well-named identifiers do that. Comments are for *why*: hidden constraints, subtle invariants, workarounds for specific external-API quirks (Notion webhook payload shape, Gmail history.list semantics, etc.). Be liberal with the *why* — those are gold for future-me.

## Common dev commands

```bash
# bring up the stack (api + postgres + cloudflared; ollama runs natively on Mac)
docker compose up -d --build

# tail application logs (filter out the /health noise)
docker compose logs -f api | grep -v "GET /health"

# run tests — ON THE HOST, NOT IN THE CONTAINER.
# `uv run pytest` builds (or reuses) a host-side .venv with dev deps and runs
# the suite against the source tree directly. Don't try to run pytest inside
# `docker compose exec api`: the production image is built with `uv sync
# --frozen --no-dev --no-editable`, so pytest is intentionally NOT in
# /app/.venv. Installing it ad-hoc inside the container also breaks subtly
# (the container has two Pythons: /usr/local/bin/python and /app/.venv/bin/
# python — `pip install` lands in the wrong one, then `python -m pytest`
# can't find it. And it's all wiped on the next --build anyway.) Tests don't
# need the container; they run against the source tree.
uv run pytest

# WINDOWS NOTE: on Tobias's Windows dev box `uv` is NOT on the shell PATH
# (PowerShell or Bash), so `uv run pytest` fails with "command not found".
# Use a one-time sidecar venv instead (pattern proven across prior chats):
#   python -m venv .venv-test
#   .\.venv-test\Scripts\python.exe -m pip install -q pytest pytest-asyncio -e .
#   .\.venv-test\Scripts\python.exe -m pytest --tb=short
# .venv-test/ is gitignored (a throwaway); reuse it if it already exists rather
# than recreating. Run from the repo root in PowerShell. NEVER `git add .` with
# it present without confirming .venv-test/ is ignored — it's thousands of files.

# reload .env (restart does NOT reload it — must --force-recreate)
docker compose up -d --force-recreate api

# see the durable queue state (pending / in_progress / done / failed)
curl http://localhost:8000/debug/queue

# re-run every terminally-failed task (after fixing the root cause)
docker compose exec api python -m gb_automations.scripts.retry_failed

# ad-hoc resync of one thread (manual/testing; normal path is queue-driven)
docker compose exec api python -m gb_automations.sync.sync_one --email USER --thread THREAD_ID

# wipe local cache and re-sync a thread from scratch (used while iterating)
docker compose exec api python -m gb_automations.scripts.reset_thread --thread THREAD_ID

# check live LLM tagging
curl 'http://localhost:8000/debug/llm?prompt=Hei,%20kan%20dere%20sende%20et%20tilbud?'
```

## Running commands on the Windows dev box (PowerShell gotchas)

The user's primary dev shell is PowerShell on Windows. Two parser quirks bite
*every* time and have already burned multiple turns:

- **`curl` is aliased to `Invoke-WebRequest`** — `-X POST` errors with "A
  parameter cannot be found that matches parameter name 'X'." Use
  `Invoke-RestMethod` (native, prints JSON nicely) or call `curl.exe`
  explicitly:

  ```powershell
  Invoke-RestMethod -Method Post http://localhost:8000/debug/toggl/sync-hours
  # or:
  curl.exe -X POST http://localhost:8000/debug/toggl/sync-hours
  ```

- **`docker compose exec api python -c "…"` with a heredoc-style multi-line
  string fails** with "ScriptBlock should only be specified as a value of the
  Command parameter." PowerShell parses the `"` continuation differently from
  bash. Two fixes that work:

  1. **Use a here-string** (preferred for >2 lines). The closing `'@` MUST be
     at column 0 (no indent — that's a parse error):

     ```powershell
     $py = @'
     import asyncio
     from gb_automations.clients import toggl
     from gb_automations.config import settings
     projects = asyncio.run(toggl.list_projects(settings.toggl_workspace_id))
     for p in projects:
         print(f"  {p['id']}  active={p.get('active')}  {p.get('name')}")
     '@
     docker compose exec api python -c $py
     ```

  2. **One-liner with `;`** (for short snippets, no f-string escaping pain):

     ```powershell
     docker compose exec api python -c "import asyncio; from gb_automations.clients import toggl; from gb_automations.config import settings; print(asyncio.run(toggl.list_projects(settings.toggl_workspace_id)))"
     ```

- **Other shorthand traps**: `2>/dev/null` → `2>$null`; `$VAR` → `$env:VAR`;
  command chaining `&&` / `||` are parser errors in PowerShell 5.1 — use
  `; if ($?) { … }`.

When proposing a command, default to the PowerShell-safe form for this user.

## Where to look when…

| Question | File |
|---|---|
| "How does a Notion event become a Gmail label?" | [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_notion_webhook_impl` ENQUEUES a `label_sync` task → worker runs [sync/sync_labels.py](src/gb_automations/sync/sync_labels.py) `sync_project_labels` (does NOT sync inline) |
| "How does a project's Status auto-trigger provisioning?" | One Notion automation on the Projects DB (`Property edited → Status`) POSTs to [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_notion_project_status_impl` — a sub-50ms ack that ONLY enqueues a `project_status_dispatch` task and returns 200. The actual work lives on the worker in [sync/dispatch_project_status.py](src/gb_automations/sync/dispatch_project_status.py) `dispatch_project_status` (Notion get_page, placeholder gate, status read, fan-out). Two-layer split exists because Notion auto-pauses webhook automations whose receiver responds too slowly (community-observed, no published timeout — the inline shape was tripping Goldbox's prod workspace). Status mapping in [config.py](src/gb_automations/config.py) `PROJECT_STATUS_AUTO_PROVISION` (cumulative: Tilbudsfase → Gmail; Tilbud godkjent → +NAS; I produksjon → +Frame +Toggl). Placeholder-title gate via `PROJECTS_PLACEHOLDER_TITLES` skips the template row `000_Kunde_Prosjekt TEMPLATE` so it doesn't mint garbage labels — recovery is rename + re-touch Status. No Name-edited automation: Notion fires Name-edited multiple times per rename (autosave + per-pause coalescing) which storms the queue with redundant `label_sync` work. Frame deliverable fan-out lives in the dispatcher; the manual Sync Frame button uses its own copy in [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_enqueue_frame_deliverables_for_project` |
| "Notion automation paused itself / how do I check?" | All three Notion-automation receivers (project-status, oppgave-done, oppgave-status) deliberately **return HTTP 200 for every code path** including auth failure and bad JSON — Notion auto-pauses on any non-2xx and the threshold is undocumented. Auth/JSON failures stay loud in `logger.warning` but Notion only ever sees 200. Operator visibility: `GET /debug/notion-automation-health` returns `{automations: {<name>: {last_seen_utc, last_action, count}}}` from an in-memory tracker updated by every receiver call ([routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_record_notion_automation_hit` + [routes/debug.py](src/gb_automations/routes/debug.py)). Resets on container restart. Full diagnosis flow (incl. Cloudflare Bot Fight Mode and webhook.site control test) in [docs/misc/notion-setup.md](docs/misc/notion-setup.md) Part 6e |
| "How does Ferdig/Tapt mark the Frame project inactive?" | Every status change ALSO enqueues `frame_project_status_sync` (in addition to the provisioning fan-out above). Engine: [sync/sync_frame_project_status.py](src/gb_automations/sync/sync_frame_project_status.py) reads live Notion Status → maps Ferdig/Tapt (`PROJECT_STATUS_INACTIVE_TRIGGERS` in [config.py](src/gb_automations/config.py)) to Frame V4 `status="inactive"`, everything else to `"active"`. Read-first / skip-if-same loop guard via [clients/frame.py](src/gb_automations/clients/frame.py) `set_project_status` + `get_project`. Skipped silently when there's no FrameProjectFolder cache row (project not yet provisioned in Frame). Notion-only direction — we never mirror Frame's status back to Notion. Reversible: reopening a Ferdig project by moving Status back to e.g. I produksjon auto-flips Frame back to `active`. V4 endpoint: `PATCH /v4/accounts/{aid}/projects/{pid}` with `{"data": {"status": "..."}}` |
| "How does a Gmail push get processed?" | [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_gmail_webhook_impl` ENQUEUES → [sync/queue.py](src/gb_automations/sync/queue.py) `enqueue_threads` (does NOT sync inline) |
| "What actually runs the sync?" | [jobs/queue_worker.py](src/gb_automations/jobs/queue_worker.py) drains the queue → [sync/sync_thread.py](src/gb_automations/sync/sync_thread.py) `sync_thread` |
| "How do I see what's queued / pending / failed?" | `GET /debug/queue`; the `sync_tasks` table is the truth |
| "How do I re-run a failed task / re-sync one thread?" | `POST /debug/queue/retry-failed` (or `scripts/retry_failed.py`); per-email rebuild via `POST /webhooks/notion/resync-thread` → `resync_project.rebuild_thread` |
| "How does the Projects DB show sync status?" | icon Select written by the worker; `PROJECTS_SYNC_STATUS` env + `PROJECTS_SYNC_PROP`/`PROJECT_SYNC_*` in [config.py](src/gb_automations/config.py) |
| "What's the optional Notion 'Sync Queue' mirror?" | `SYNC_QUEUE_DB_ID` env + `SYNC_QUEUE_PROPS` in [config.py](src/gb_automations/config.py); writer in [sync/queue_mirror.py](src/gb_automations/sync/queue_mirror.py) |
| "A stale Notion id is causing 404 / 'archived ancestor' errors?" | self-heal: `NotionAPIError.is_stale_object` + `page_is_live` in [clients/notion.py](src/gb_automations/clients/notion.py); cache eviction in [sync/sync_thread.py](src/gb_automations/sync/sync_thread.py) + [clients/notion_emails_db.py](src/gb_automations/clients/notion_emails_db.py) |
| "What does a synced row look like in Notion?" | `EMAILS_PROPS` in [config.py](src/gb_automations/config.py) — property names, types, and order |
| "What tags can the LLM apply?" | `EMAIL_TAGS` in [config.py](src/gb_automations/config.py); prompt body in [prompts/](prompts/) |
| "How are re-carried attachments kept off every reply row?" | `ThreadAttachmentTracker` (`attached_this_pass`) in [sync/sync_thread.py](src/gb_automations/sync/sync_thread.py); gotchas.md attachment notes |
| "Why is a known logo still uploading from this sender?" | per-contact byte-exact learning in `contact_signature_images` table; threshold = `settings.signature_learn_threshold` (default 3 distinct threads). Helper: `_check_or_record_signature_image` in [sync/sync_thread.py](src/gb_automations/sync/sync_thread.py); current state at `GET /debug/signatures?status=signature`. Un-learn / force-mark recipes in [gotchas.md](docs/misc/gotchas.md) §18 |
| "How does a Frame.io folder get created?" | `SYNC_FRAME=true` flag in [config.py](src/gb_automations/config.py); the per-row Sync button on a deliverable enqueues `frame_leveranse_sync` (gated on `Type` being a recognized discipline) in [routes/webhooks.py](src/gb_automations/routes/webhooks.py) → worker runs [sync/sync_frame.py](src/gb_automations/sync/sync_frame.py) `sync_frame_project` / `sync_frame_leveranse` |
| "How is the Frame V00 placeholder image generated?" | Dynamically per-deliverable: `sync_frame_leveranse` points Frame's `create_file_from_url` at `GET /assets/placeholder/{page_id}.png` ([routes/assets.py](src/gb_automations/routes/assets.py)) instead of a static file. The endpoint reads the deliverable's `Beskrivelse` (`OPPGAVER_DESC_PROP`, falls back to title) + `Thumbnail` (`OPPGAVER_THUMB_PROP`) live and renders via Pillow ([sync/placeholder_image.py](src/gb_automations/sync/placeholder_image.py)). Origin derived from `frame_placeholder_url` (`settings.placeholder_render_base`); the static URL is the fallback. Public + unauth (Frame fetches over the tunnel); degrades to a black PNG on any error |
| "How are Frame URLs written back to Notion?" | `set_project_frame_url` / `set_deliverable_frame_url` in [clients/notion.py](src/gb_automations/clients/notion.py); `PROJECTS_FRAME_URL_PROP` / `OPPGAVER_FRAME_URL_PROP` in [config.py](src/gb_automations/config.py) |
| "How do Frame comments become Notion rows?" | `POST /webhooks/frame` (`comment.created`) enqueues `frame_comment_sync` → worker runs [sync/sync_frame_comments.py](src/gb_automations/sync/sync_frame_comments.py) `sync_frame_comment` → creates the `Korreksjonsrunde N` sub-row under the deliverable (Oppgaver DB) and a `Korreksjon` row in the Korreksjoner DB related to it (or nested under the parent comment's Korreksjon for replies). Cache: `FrameComment` table |
| "Where does the deliverable Status come from?" | Auto-managed by 3 engines: [sync/sync_frame_version.py](src/gb_automations/sync/sync_frame_version.py) (Ferdig), [sync/sync_frame_comments.py](src/gb_automations/sync/sync_frame_comments.py) (Klar til oppstart on first comment), [sync/sync_leveranse_status.py](src/gb_automations/sync/sync_leveranse_status.py) (Under arbeid / Oppgaver ferdig rollup). All gated by `notion_client.set_deliverable_status` which respects `MANUAL_DELIVERABLE_STATUSES` |
| "How does Ferdig sync 2-way between Notion and Frame?" | Notion → Frame: `POST /webhooks/notion/oppgave-done` (bearer auth, fires on a Korreksjon row's Ferdig toggle in the Korreksjoner DB) enqueues `oppgave_done_sync` → [sync/sync_oppgave_done.py](src/gb_automations/sync/sync_oppgave_done.py) PATCHes Frame via `frame.set_comment_completed`. Frame → Notion: `comment.completed` webhook is handled by the existing comment engine which propagates to `set_row_done`. Loop-prevention via read-first/skip-if-same on both helpers |
| "Frame.io setup / bootstrap?" | [docs/misc/frame-setup.md](docs/misc/frame-setup.md); scripts in [scripts/frame_oauth_bootstrap.py](src/gb_automations/scripts/frame_oauth_bootstrap.py) (one-time OAuth) and [scripts/frame_register_webhook.py](src/gb_automations/scripts/frame_register_webhook.py) (re-runnable webhook registration); smoke tests at `GET /debug/frame` + `GET /debug/frame/workspace` + `GET /debug/frame/webhooks` |
| "How does the 'Send faktura' button work?" | One Notion button on the Projects DB → `POST /webhooks/notion/send-faktura` (bearer auth) enqueues a `send_faktura` task → worker dispatches to [sync/sync_fiken_invoice.py](src/gb_automations/sync/sync_fiken_invoice.py) `create_fiken_invoice(project_page_id)`. The engine reads the project's `Faktura status` (`PROJECTS_FAKTURA_STATUS_PROP` in [config.py](src/gb_automations/config.py)) to decide oppstart vs slutt; non-billable states skip cleanly. Dedup keys on `project_page_id` alone (one billable state at a time), so double-clicks collapse |
| "How does the 'Sjekk fiken' button work?" | A second Notion button on Projects, same shape as Send faktura. `POST /webhooks/notion/graduate-faktura` (same bearer auth) enqueues a `graduate_faktura` task → worker dispatches to [sync/graduate_project.py](src/gb_automations/sync/graduate_project.py) `graduate_project(project_page_id)`. The engine lists every sent invoice on the Fiken side, matches each to this project via THREE strategies in order: (1) `draft_uuid → invoiceDraftUuid` FK (invoices we originated via Send faktura), (2) `ourReference` casefold-exact (Vår referanse), (3) `invoiceText` casefold-exact (Kommentar — the operational anchor since the CEO writes meaningful Kommentar on every invoice even when Vår referanse is blank; name the Notion project the same string as the Kommentar to pull historical invoices). Then writes Faktura DB rows, graduates statuses + Fakturert beløp (draft_uuid path only — the two fallbacks have no per-Oppgave audit trail so they skip Oppgave-status writes), clears draft URL. **Also lists `/creditNotes`** and lands them in the Faktura DB as `Type=Kreditnota` (matched via `associatedInvoiceId → FikenInvoice` primary, `kommentar_parent` = parse "Kreditnota for faktura {N}" out of the Kommentar and find the parent FikenInvoice by invoice_number, or ourReference/creditNoteText fallbacks). When a kreditnota fully reverses its parent (abs(net) == parent.net), the engine subtracts the per-Oppgave NOK from `Fakturert beløp` and flips Oppgave `Fakturert status = Kreditert`. Project-level `Faktura status = Kreditert` is auto-set at the end of `graduate_project` iff every billable Oppgave on the project is Kreditert (Utgår rows are ignored as opt-outs). Idempotent: `sent_at` marker on FikenInvoice short-circuits re-graduation of fakturas, FakturaNotionCache short-circuits Faktura DB writes, and the kreditnota reversal also short-circuits on cache hit so re-running Sjekk fiken doesn't double-subtract Fakturert beløp. URLs: the `URL` column uses Fiken's `/handel/salg/{saleId}` page (pinned empirically — the `/faktura/{id}` and `/kreditnota/{id}` paths return blank pages, `saleId` lives on `record["sale"]["saleId"]`). The `Faktura PDF` column is the direct PDF download, mapped from `invoicePdf.downloadUrlWithFikenNormalUserCredentials` (invoices) or `creditNotePdf.downloadUrlWithFikenNormalUserCredentials` (kreditnotas) — the browser-friendly URL that auto-authenticates via the operator's Fiken session. |
| "Is there an auto-poller?" | Yes — C.3b shipped. When `SYNC_FIKEN_GRADUATIONS=true` in `.env`, an APScheduler cron at minute :07 every hour ([jobs/scheduler.py](src/gb_automations/jobs/scheduler.py) `_enqueue_fiken_graduations_for_all_active_projects`) queries the Projects DB and enqueues a `graduate_faktura` task per project whose `Faktura status` is non-terminal (not blank / Ikke fakturert / Fakturert / Kreditert). Each per-project task runs the same `graduate_project` engine the button uses. Per-project dedup collapses cron-enqueue with operator clicks. Logs one summary line per fire: `⏰ fiken_graduations cron: scanned=N enqueued=K skipped_terminal=T skipped_dedup=D errors=E`. CEO sends an invoice in Fiken → within ~1 hour Notion auto-graduates without anyone touching the button. |
| "Which Oppgaver get invoiced + for how much?" | `_eligible_rows` + `_row_billable_nok` in [sync/sync_fiken_invoice.py](src/gb_automations/sync/sync_fiken_invoice.py). Eligibility = recognized discipline + `Fakturert status` not in {Fakturert, Utgår} (oppstart needs `Ikke fakturert`; slutt also takes `Fakturert 50%`). Per-row math: `gross = Pris × (1 − Rabatt/100)`, `remaining = gross − Fakturert beløp`, `to_bill = round(gross × 0.5)` for oppstart or `round(remaining)` for slutt. Rows with no Pris or remaining ≤ 0 skip with a WARN. Inspect what the engine sees live: `GET /debug/fiken/inspect?project_page_id=…` |
| "How does Notion track what's been invoiced?" | `Fakturert beløp` (number NOK) on each Oppgave is the engine-written running total. After each successful draft `notion_client.set_oppgave_billed` writes the new total + flips `Fakturert status` to `Fakturert 50%` (oppstart) or `Fakturert` (slutt). Project-level sum is a Notion rollup of `Fakturert beløp` across the project's Oppgaver — engine never writes the project total. Audit trail in Postgres: `FikenInvoice` + `FikenInvoiceLine` (the `billed_amount_ore` column is the absolute NOK per draft, in øre — v1 stored a fraction but Pris is mutable so a fraction would drift) |
| "Why isn't my new integration working?" | [docs/misc/gotchas.md](docs/misc/gotchas.md) first, then the relevant client wrapper in `clients/` |
| "What's the full deployment story for a fresh workspace?" | [docs/guide.md](docs/guide.md) → [google-setup.md](docs/misc/google-setup.md) + [scripts/gcp-bootstrap.sh](scripts/gcp-bootstrap.sh) + [notion-setup.md](docs/misc/notion-setup.md) + [cloudflare-setup.md](docs/misc/cloudflare-setup.md) |
| "What does the client actually want long-term?" | [docs/reference/client-brief.md](docs/reference/client-brief.md) |
