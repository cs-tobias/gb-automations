# gb-automations — orientation

Long-term goal: **Notion is the single source of truth** for everything that happens at Goldbox (a Norwegian architectural-visualization studio). This repo is the always-on backend that pulls data from every tool the team uses into Notion, and keeps it bidirectionally in sync where it makes sense. Once the context is in Notion, AI can reason over the whole business.

Full client vision in [docs/reference/client-brief.md](docs/reference/client-brief.md) (Norwegian) — read it for the *why* behind any integration.

## Current state (May 2026)

**Gmail ↔ Notion sync is live and in production** on `tobias@cinesuit.com`'s inbox. Stages 1–4c of the README roadmap are done.

What works end-to-end:
- New row in Notion `Project` DB → Gmail label created in every seeded user's mailbox (and renamed when the project is renamed in Notion).
- Email labeled with a project label in Gmail → Pub/Sub push → email rows appear in Notion `Emails` DB, linked to the project, with: cleaned body, extracted history (split into per-message rows), participants upserted to `Contacts` DB, attachments uploaded to Drive and linked, multi-select tags applied by a local Ollama LLM (taxonomy in [src/gb_automations/config.py](src/gb_automations/config.py) `EMAIL_TAGS`).

## Next up: Frame.io integration (week of 2026-05-18)

Expected to be **~2× the size of the Gmail+Notion work combined**. Scope from the client brief:
- Project created & marked Active in Notion → mirror to Frame.io (including renames).
- Task structure in Notion (e.g. 3 exteriors + 4 interiors) → folder + placeholder file structure in Frame.io.
- Frame.io comments → sync into Notion as a `Corrections` database, linked to project + task.
- Auto-create "korreksjon runde N" sub-tasks under the parent task when a new correction round arrives.
- Eventually: AI drafts replies to Frame.io comments using mail/brief context, project manager approves before sending.
- Project marked finished in Notion → set inactive in Frame.io.

After Frame: Toggl (daily aggregated hours → Notion), Fiken (accounting), meeting transcripts, then MCP server + RAG.

## Architecture in one diagram

```
                   ┌─────────────────────────────────┐
                   │  FastAPI (src/gb_automations)   │
                   │  ───────────────────────────    │
   Notion ────────►│  /webhooks/notion               │
   webhook         │     → create/rename Gmail label │
                   │                                 │
   Gmail Pub/Sub ─►│  /webhooks/gmail                │
   push            │     → sync_thread()             │
                   │     → write rows to Notion      │
                   │                                 │
                   │  APScheduler                    │
                   │     → renew Gmail watches /5d   │
                   └──────────┬──────────────────────┘
                              │
                ┌─────────────┼─────────────┐
                ▼             ▼             ▼
            Postgres     Ollama        secrets/
            (dedup       (tagging       gcp-service-
             cache,       LLM)          account.json
             cursors)
```

Everything runs in Docker Compose. Public traffic enters via Cloudflare Tunnel at `https://hub.<domain>/…`. The full deployment runbook is [docs/setup.md](docs/setup.md); painful lessons captured in [docs/gotchas.md](docs/gotchas.md) — **always check gotchas.md before debugging an integration issue**, most have an entry.

## Repo layout

```
src/gb_automations/
  main.py             FastAPI app, logging config, lifespan
  config.py           pydantic-settings, EMAILS_PROPS, EMAIL_TAGS taxonomy
  db.py               async SQLAlchemy engine + SessionLocal
  models.py           SyncCursor, User, EmailRow, ContactCache, AttachmentFingerprint, ProjectLabel
  obs.py              request_scope() — stamps logs with [prefix:abcd] per webhook
  routes/
    webhooks.py       /webhooks/{echo,notion,gmail} — the entry points
    debug.py          /debug/{databases,notion,llm} — diagnostic endpoints
  clients/
    gmail.py          DWD-impersonated Gmail wrapper (sync)
    notion.py         Notion REST wrapper (async httpx)
    drive.py          Drive uploads for email attachments (sync)
    llm.py            Ollama tagging — loads prompt from prompts/*.md
  sync/
    sync_thread.py    THE big one — Gmail thread → Notion rows (~1200 LOC)
    sync_one.py       CLI wrapper for ad-hoc resync of one thread
    watches.py        Gmail users.watch() lifecycle
  utils/
    email_cleaning.py   strip signatures, quoted history, etc.
    email_splitting.py  split forwarded chains into individual messages
    history_extraction.py  regex-based "On X wrote:" detection (replaced LLM splitter)
    participants.py   parse From/To/Cc, internal-vs-external classification
    phone.py          extract NO/intl phone numbers from signatures
  jobs/scheduler.py   APScheduler — renews Gmail watches
  scripts/            one-shot CLIs (seed_users, start_watches, backfill_project_labels,
                      pull_llm_model, reset_thread)
prompts/
  default.md          generic tagging prompt
  goldbox.md          Goldbox-specific tagging prompt (set TAGGING_PROMPT_PATH)
docs/
  setup.md            fresh-deployment runbook (GCP + Workspace + Notion + Cloudflare)
  gotchas.md          11+ entries of "this cost me hours, here's the fix"
  reference/          client brief, original Apps Script, prior Claude design chats, real logs
migrations/           Alembic (sync engine for migrations; app uses async)
tests/                pytest — unit tests for cleaning/splitting/extraction/participants
```

## Key conventions

- **Python ≥3.12, async FastAPI + async SQLAlchemy**. Sync Google client calls go through `asyncio.to_thread` / a thread pool — don't block the event loop.
- **Dedup belongs in Postgres, truth in Notion.** `EmailRow` / `ContactCache` are local caches to avoid re-querying Notion; we still write to Notion on every change.
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

# run tests
uv run pytest

# reload .env (restart does NOT reload it — must --force-recreate)
docker compose up -d --force-recreate api

# ad-hoc resync of one thread
docker compose exec api python -m gb_automations.sync.sync_one --email USER --thread THREAD_ID

# wipe local cache and re-sync a thread from scratch (used while iterating)
docker compose exec api python -m gb_automations.scripts.reset_thread --thread THREAD_ID

# check live LLM tagging
curl 'http://localhost:8000/debug/llm?prompt=Hei,%20kan%20dere%20sende%20et%20tilbud?'
```

## Where to look when…

| Question | File |
|---|---|
| "How does a Notion event become a Gmail label?" | [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_notion_webhook_impl` |
| "How does a Gmail push become Notion rows?" | [routes/webhooks.py](src/gb_automations/routes/webhooks.py) `_gmail_webhook_impl` → [sync/sync_thread.py](src/gb_automations/sync/sync_thread.py) `sync_thread` |
| "What does a synced row look like in Notion?" | `EMAILS_PROPS` in [config.py](src/gb_automations/config.py) — property names, types, and order |
| "What tags can the LLM apply?" | `EMAIL_TAGS` in [config.py](src/gb_automations/config.py); prompt body in [prompts/](prompts/) |
| "How are signatures detected so they don't bloat Drive?" | `AttachmentFingerprint` model + the `(sender, content-sha1) seen_count ≥ 2` rule |
| "Why isn't my new integration working?" | [docs/gotchas.md](docs/gotchas.md) first, then the relevant client wrapper in `clients/` |
| "What's the full deployment story for a fresh workspace?" | [docs/setup.md](docs/setup.md) — top to bottom, ~1 hour |
| "What does the client actually want long-term?" | [docs/reference/client-brief.md](docs/reference/client-brief.md) |
