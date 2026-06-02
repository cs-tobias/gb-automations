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
| "Why isn't my new integration working?" | [docs/misc/gotchas.md](docs/misc/gotchas.md) first, then the relevant client wrapper in `clients/` |
| "What's the full deployment story for a fresh workspace?" | [docs/guide.md](docs/guide.md) → [google-setup.md](docs/misc/google-setup.md) + [scripts/gcp-bootstrap.sh](scripts/gcp-bootstrap.sh) + [notion-setup.md](docs/misc/notion-setup.md) + [cloudflare-setup.md](docs/misc/cloudflare-setup.md) |
| "What does the client actually want long-term?" | [docs/reference/client-brief.md](docs/reference/client-brief.md) |
