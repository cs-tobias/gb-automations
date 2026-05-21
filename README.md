# gb-automations

Goldbox automation hub. Long-term goal: make Notion the single source of truth across Gmail, Frame.io, Toggl, Fiken, meeting transcripts, and eventually local-LLM RAG. This repo is the always-on backend that ties them together.

> New here? Read [CLAUDE.md](CLAUDE.md) first — it's the orientation map (architecture, repo layout, where to look for what).

## Current state

**Gmail ↔ Notion sync is live and in production.** What works end-to-end today:

- New project row in Notion → Gmail label created in every seeded mailbox (renamed when the project is renamed).
- Email labeled with a project label in Gmail → Pub/Sub push → a **durable sync task** is enqueued (Postgres `sync_tasks`), and a background **worker** drains the queue: cleaned body, extracted per-message history, participants upserted to Contacts/Companies, attachments uploaded to Drive, multi-select tags applied by a local Ollama LLM, all written to a year-partitioned Notion `Emails` DB.
- The queue guarantees delivery: crash-safe, retry-with-backoff, terminal failures parked as `failed` (never silently lost). Per-project sync status shows as an icon on the Projects DB (`PROJECTS_SYNC_STATUS`); an optional Notion "Sync Queue" mirror (`SYNC_QUEUE_DB_ID`) shows live queue state.
- Optional: office NAS project folders (`SYNC_NAS_FOLDERS`).

Next up per the roadmap: Frame.io. See [CLAUDE.md](CLAUDE.md) for the live picture and [docs/reference/client-brief.md](docs/reference/client-brief.md) for the full vision.

## Quick start

```bash
cp .env.example .env       # then fill in the values
docker compose up --build
```

The api container runs `alembic upgrade head` on start, so migrations apply automatically. Health check:

```bash
curl http://localhost:8000/health
# → {"status":"ok","env":"dev","db":"ok"}
```

Queue state (what's pending / processing / failed):

```bash
curl http://localhost:8000/debug/queue
```

Stop with `docker compose down`. Add `-v` to also wipe the Postgres volume (the queue + dedup caches live there; truth is in Notion).

## Fresh deployment

See **[docs/guide.md](docs/guide.md)** — the short checklist that fans out to the Google, Notion, and Cloudflare setup docs. (An interactive installer was abandoned; ignore `docs/misc/setup.md`, `docs/misc/setup-manual.md`, and `scripts/setup_workspace.py`.)

## Local development without Docker

`uv` manages Python and deps:

```bash
uv sync
uv run uvicorn gb_automations.main:app --reload
```

You'll need a Postgres reachable at the `DATABASE_URL` in `.env` (or run just the `db` service: `docker compose up db`). Ollama runs natively on the host.

## Tests

```bash
uv run pytest
```

Note: `tests/test_health.py` hits a running stack on `http://localhost:8000` and fails if the stack isn't up — that one is expected to fail in a bare `uv run pytest`; the rest are pure unit tests.

## Structure

See the repo-layout section of [CLAUDE.md](CLAUDE.md) for the annotated file map. In brief:

```
src/gb_automations/
  main.py        FastAPI app, logging, lifespan (starts the queue worker)
  routes/        webhooks (notion, gmail, resync-thread), debug (/debug/queue)
  sync/          sync_thread (the engine), queue + queue_worker (durable queue),
                 queue_mirror, resync_project, backfill_project_labels
  clients/       gmail, notion, drive, llm, nas
  jobs/          scheduler (Gmail watch renewal), queue_worker
  scripts/       one-shot CLIs (seed_users, start_watches, reconcile, retry_failed, …)
migrations/      Alembic
docs/            guide.md + setup docs + gotchas.md
```

## Common commands

```bash
docker compose up -d --build              # bring up the stack
docker compose logs -f api                # tail logs (colored, queue-narrated)
docker compose up -d --force-recreate api # reload .env (restart alone won't)
uv run pytest                             # tests
```
