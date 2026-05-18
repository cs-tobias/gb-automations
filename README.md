# gb-automations

Goldbox automation hub. Long-term goal: make Notion the single source of truth across Gmail, Frame.io, Toggl, Fiken, meeting transcripts, and eventually local-LLM RAG. This repo is the always-on backend that ties them together.

## Stage 1 — Skeleton (current)

Just the foundation: FastAPI + Postgres in Docker Compose, Alembic for migrations, a `/health` endpoint that proves the stack is wired up. No integrations yet.

## Quick start

```bash
cp .env.example .env
docker compose up --build
```

Then in another terminal:

```bash
curl http://localhost:8000/health
# → {"status":"ok","env":"dev","db":"ok"}
```

Stop with `docker compose down`. Add `-v` to also wipe the Postgres volume.

## Local development without Docker

`uv` manages Python and deps:

```bash
uv sync
uv run uvicorn gb_automations.main:app --reload
```

You'll need a Postgres reachable at the `DATABASE_URL` in `.env` (or run just the `db` service: `docker compose up db`).

## Tests

```bash
uv run pytest
```

The smoke test hits a running stack on `http://localhost:8000` — start with `docker compose up` first.

## Structure

```
src/gb_automations/   FastAPI app, config, db, models
migrations/           Alembic migrations
tests/                pytest
docs/reference/       Prior architecture chat + the existing Apps Script
                      (kept for porting to Python in Stage 2)
```

## Roadmap

1. **Stage 1 — skeleton** ← here
2. **Stage 2** — port the Apps Script Gmail↔Notion sync to Python, run on a schedule (APScheduler) inside the same FastAPI process. Apps Script keeps running in parallel until parity is verified.
3. **Stage 3** — first webhook (Notion `page.created` → create Gmail label). Adds Cloudflare Tunnel.
4. **Stage 4** — Gmail Pub/Sub push for the email side. Domain-wide delegation for multi-inbox.
5. **Stage 5+** — Frame, Toggl, Fiken, meeting transcripts, MCP server, RAG.

See [docs/setup.md](docs/setup.md) for the one-command interactive installer (`python -m gb_automations.scripts.setup_workspace` — ~15 min of clicks for a fresh workspace), [docs/setup-manual.md](docs/setup-manual.md) for the long-form click-by-click guide the installer automates, [docs/gotchas.md](docs/gotchas.md) for pitfalls both guides prevent, and [docs/reference/claude-chat.md](docs/reference/claude-chat.md) for the architecture conversation that led here.
