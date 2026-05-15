I'll compare what we shipped against the brief. The brief covers five integrations — we built one (Gmail). Here's the honest gap analysis.

Gmail scope (what the brief asks for vs what's shipped)
Brief item	Status
Notion project → Gmail label, in ALL Goldbox mailboxes	✅ Done
Apply label → email syncs into Notion Emails DB, linked to project	✅ Done
Notion project rename → Gmail label rename across all mailboxes	✅ Done (just shipped)
Auto-extract contacts (name, company, email, phone) from threads → Contacts DB	✅ Done
Email threads grouped/visible per conversation in Notion	✅ Done (chat-style callout blocks per message, Thread ID property)
Dedup, correct ordering, no double rows	✅ Done (message_id PK + internalDate ordering)
Auto-tagging ("tilbud", "korreksjon" so users can filter)	❌ Missing. EMAILS_PROPS has a Tags slot defined but nothing populates it. Would be a small Anthropic-backed classifier on the cleaned body, writing a multi-select.
Inline images in emails preserved in Notion view	❌ Missing. clean_body strips HTML; inline cid: images are lost. Attachments are listed as text-only. The brief explicitly flags this as a concern.
So Gmail is ~90% there. Two real gaps: auto-tagging and inline-image handling.

Other sections in the brief — not started
Section	Status
Frame.io — project mirroring (active project → Frame), folder/placeholder structure for tasks, comment sync to a Corrections DB, sub-tasks per correction round	❌ Not started
Toggl — project mirroring, daily roll-up sync (per employee × project × day) into Notion, payroll grounding	❌ Not started
Fiken — accounting integration	❌ Not started (also empty in the brief itself, undefined scope)
Meeting transcription — capture audio from physical/online meetings → transcribe → Notion	❌ Not started
Long-term: RAG / LLM-Wiki on top of Notion as context source	❌ Not started (architectural goal, not a near-term feature)
Implicit cross-cutting items the brief mentions
Project lifecycle: "active" vs "tilbud" (offer phase). Frame/Toggl integrations both gate on this — only mirror once the project becomes active. We don't currently distinguish: Gmail labels are created the moment any project row appears in Notion, regardless of status. Worth knowing for when Frame/Toggl land — they'll probably need a Status property on the Projects DB to read.
Project "ferdig" (complete) → inactive everywhere. Frame/Toggl care; Gmail probably doesn't (labels don't have an inactive state, leaving them alone is fine).
Recommendation
For Gmail specifically, the two real follow-ups are auto-tagging and inline images. Auto-tagging is a clean ~50-line addition (Anthropic call on body, write multi-select). Inline images are messier — Notion can't embed cid: images directly; you'd have to either (a) convert to attachments and upload to Drive/Notion, or (b) skip silently. Worth deciding before starting.

For everything else: it's separate-stage work, each one a project of its own scope. None blocks the Gmail piece going to production.

Want to do auto-tagging next, tackle one of the bigger integrations (Frame is the biggest customer-facing payoff), or treat the Goldbox handoff as the next milestone and queue the rest for after?

yeah. i thimk the next natural step is the LLM. And, since we have the local docker setup now, im thinking; lets add a local LLM? this way, we can use this to run LLM calls free. and we want to use this for other stuff later too. 

How should we proceed here?
Let me peek at the existing client patterns and deps to ground the plan, then I'll ask you a couple of key questions before writing it up.

Bash Check current deps for LLM-related packages
IN
grep -E "^name|^dependencies|anthropic|httpx" /Users/tobiaseek/Documents/Code/gb-automations/pyproject.toml | head -30

OUT
name = "gb-automations"
dependencies = [
    "httpx>=0.28.1",
    "httpx>=0.28.1",
Bash List existing client modules
IN
ls /Users/tobiaseek/Documents/Code/gb-automations/src/gb_automations/clients/

OUT
__init__.py
__pycache__
gmail.py
notion.py
The existing client pattern (gmail.py, notion.py — httpx-based, async) gives me a clean slot for an llm.py. But the architecture genuinely depends on hardware — local LLMs are GPU-bound, and Mac vs Linux changes whether Ollama can run inside docker or has to live on the host. Let me get a few things straight before writing the plan.

AskUserQuestion
OUT
User has answered your questions: "What's the office PC that this will eventually run on?"="the pc will be a windows workstation. with, i think, we can access 16gb or 24gb. but i think the 16gb one is the one we should get, becuase yeah. so, a decent workstation. rtx 4080 16gb vram. ", "What should be the first feature powered by the local LLM?"="Email auto-tagging (recommended)", "If a task is too big or sensitive for the local model, do you want an Anthropic API fallback?"="Local-only for now". You can now continue with the user's answers in mind.
Got it. With an RTX 4080 (16GB VRAM) on prod and local-only, this is a clean plan. Writing it up.

Write hi-please-look-at-linear-wall.md
216 lines
# Local LLM (Ollama) + email auto-tagging as the first use case

## Context

The client brief explicitly asks for auto-tagging on synced emails ("tilbud", "korreksjon", etc.) so users can filter the Notion Emails DB by category. It also points toward a longer-term direction where Notion becomes context for AI workflows (RAG, project-log summaries, draft replies, etc.) — all of which want a cheap, private, always-available LLM in the same compose stack as the api.

Production target is a Windows workstation with an RTX 4080 16GB. Dev machine is Tobias' MacBook. The plan: add Ollama as a docker-compose service, add a thin async `llm` client, and use it to populate the currently-unwritten `Tags` multi-select on the Emails DB. No Anthropic fallback — local only, free.

## Decisions

- **Runtime**: Ollama, official `ollama/ollama:latest` image. Exposes an OpenAI-style HTTP API at port 11434, has built-in JSON-schema-constrained output (perfect for classification), and a one-line model-pull command. Model weights persist in a docker volume so rebuilds don't re-download.
- **Default model**: `llama3.1:8b-instruct-q4_K_M` — ~5GB VRAM footprint on a 4080, fast enough for inline tagging (~1-2s per email), strong enough for short-text classification. Configurable via env so we can swap to Qwen or a larger model later without code changes.
- **GPU on Windows prod, CPU on Mac dev**: base `docker-compose.yml` runs Ollama CPU-only (works everywhere). A new `docker-compose.gpu.yml` override file adds the nvidia GPU reservation for the prod machine. Mac dev gets correctness, not speed (sync still works, tagging just takes 5-10s instead of 1s).
- **Tag taxonomy is config-driven**: a list in `config.py` that's easy to edit. Notion auto-creates new multi-select options when we write a tag value that doesn't exist yet, so we don't need a pre-creation step in Notion.
- **Failure isolation**: if Ollama is unreachable or returns garbage, tagging silently no-ops on that email — the row still gets created with all other properties. Tagging is a nice-to-have; sync correctness comes first.

## Changes

### 1. `docker-compose.yml` — add the Ollama service

```yaml
ollama:
  image: ollama/ollama:latest
  ports:
    - "11434:11434"
  volumes:
    - ollama_data:/root/.ollama
  restart: unless-stopped

# in the volumes block at the bottom:
volumes:
  db_data:
  ollama_data:
```

The api service already reads from `.env`; new env vars `OLLAMA_BASE_URL=http://ollama:11434` and `OLLAMA_MODEL=llama3.1:8b-instruct-q4_K_M` go in `.env.example` so the api container resolves "ollama" via compose's internal DNS.

### 2. `docker-compose.gpu.yml` — new override for Windows prod

```yaml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

Used as:
```bash
# Windows prod:
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
# Mac dev (or any machine without nvidia-container-toolkit):
docker compose up -d --build
```

### 3. `src/gb_automations/config.py` — new settings + tag taxonomy

```python
# Local LLM (Ollama) settings
ollama_base_url: str = "http://ollama:11434"
ollama_model: str = "llama3.1:8b-instruct-q4_K_M"
ollama_timeout_s: float = 30.0  # tagging is short; 30s covers cold-start
```

Plus a module-level constant for the tag list (Norwegian terms, matching the brief):

```python
EMAIL_TAGS = [
    "tilbud",       # offer/quote
    "bestilling",   # confirmed order
    "korreksjon",   # correction round
    "leveranse",    # delivery / final files
    "spørsmål",     # question/inquiry
    "underlag",     # briefing material/specs
    "møte",         # meeting
    "faktura",      # invoice
    "intern",       # internal Goldbox communication
    "annet",        # other / uncategorized
]
```

### 4. New `src/gb_automations/clients/llm.py`

Mirror the shape of [clients/notion.py](src/gb_automations/clients/notion.py) — module-level helpers using a shared async httpx client. Single public function for now:

```python
async def classify(prompt: str, allowed_values: list[str]) -> list[str]:
    """Call Ollama with JSON-schema-constrained output.

    Returns a subset of `allowed_values`. Empty list on any failure
    (network, timeout, model unavailable, malformed response) — caller
    decides whether to fall back gracefully.
    """
```

Internally:
- POST to `{base_url}/api/chat` with `format: {"type":"object","properties":{"tags":{"type":"array","items":{"type":"string","enum":allowed_values}}},"required":["tags"]}` so Ollama constrains output to valid JSON matching the schema.
- System prompt: short, in Norwegian/English, instructs the model to pick tags relevant to the message.
- Parse the response, validate every returned tag is in `allowed_values` (defense against the model going off-rails), return the filtered set.
- Wrap the whole call in a try/except — log + return `[]` on any error.

### 5. Wire tagging into the email sync — `src/gb_automations/sync/sync_thread.py`

In `_build_email_row_properties()`, after building `body`:

```python
tags: list[str] = []
if body and "tags" in EMAILS_PROPS and EMAILS_PROPS["tags"] in emails_db_props:
    tags = await llm_client.classify(
        prompt=f"Subject: {msg.subject}\n\n{body[:3000]}",
        allowed_values=EMAIL_TAGS,
    )
maybe_set("tags", {"multi_select": [{"name": t} for t in tags]})
```

(Function becomes async — already runs inside an async context from `_sync_message`.) Truncating body to 3000 chars keeps prompt size predictable.

### 6. Bootstrap script — `src/gb_automations/scripts/pull_llm_model.py`

One-shot to pull the model into the Ollama volume:

```python
# POST {OLLAMA_BASE_URL}/api/pull with {"name": settings.ollama_model}
# Streams progress lines; print them so user can watch it pull (~5GB).
```

Used after first `docker compose up -d`. Idempotent — pulling an already-present model is a no-op.

### 7. Debug route — `src/gb_automations/routes/debug.py`

Add `/debug/llm?prompt=...` that calls `llm_client.classify()` against `EMAIL_TAGS` and returns the result + raw response. Lets the user verify Ollama is reachable and the model is loaded without sending a real email.

### 8. `docs/setup.md` — new step

Insert between current step 11 (seed users + start watches) and step 12 (verify):

```
## 11b. Pull the local LLM model (one-time, ~5 min)

The Ollama container starts empty — first `docker compose up` doesn't include the model weights.
Pull them once:

    docker compose exec api python -m gb_automations.scripts.pull_llm_model

Streams download progress (~5GB). Stored in the `ollama_data` docker volume, so it
survives rebuilds. Re-run if you swap OLLAMA_MODEL in .env.

> ⚠️ On Windows prod with the RTX 4080, you also need to start the stack with the
> GPU override so Ollama uses the GPU:
>     docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
> On Mac dev, just `docker compose up -d --build` — Ollama runs CPU-only (slower
> but functional). Verify GPU is in use on prod:
>     docker compose exec ollama nvidia-smi
```

Also add an entry to the Recovery table at the bottom:

| Tagging stopped working | `curl https://hub.../debug/llm?prompt=test` — if it errors, `docker compose restart ollama` and re-pull the model |

## Critical files

- [docker-compose.yml](docker-compose.yml) — new ollama service + named volume
- New `docker-compose.gpu.yml` — GPU override (Windows prod only)
- [src/gb_automations/config.py](src/gb_automations/config.py) — Ollama settings + `EMAIL_TAGS` taxonomy
- New [src/gb_automations/clients/llm.py](src/gb_automations/clients/llm.py) — `classify()` wrapping Ollama's `/api/chat` with JSON-schema-constrained output
- [src/gb_automations/sync/sync_thread.py](src/gb_automations/sync/sync_thread.py) — `_build_email_row_properties` becomes async, calls `llm_client.classify()` to populate `Tags`
- New [src/gb_automations/scripts/pull_llm_model.py](src/gb_automations/scripts/pull_llm_model.py)
- [src/gb_automations/routes/debug.py](src/gb_automations/routes/debug.py) — `/debug/llm` smoke endpoint
- [docs/setup.md](docs/setup.md) — model pull step + GPU override note + recovery row

## Reuse, not re-invent

- httpx async client pattern from [clients/notion.py](src/gb_automations/clients/notion.py) — same shape for `clients/llm.py`
- Schema introspection via `get_emails_db_property_names()` already gates property writes on what exists in Notion — so if the user hasn't added a `Tags` multi-select column yet, tagging silently no-ops, no special handling needed
- `maybe_set()` closure pattern in `_build_email_row_properties` — tags slot was always defined in `EMAILS_PROPS`, just never populated; the existing pattern handles it for free

## Out of scope for this plan

- Other LLM use cases (project log summaries, contact enrichment, Frame.io reply suggestions, RAG over Notion content). All useful, all build on top of the same `llm_client.classify()` (or a sibling `chat()` helper added later). Stage them after auto-tagging is proven on real traffic.
- Anthropic fallback — explicit user decision: local-only for now.
- Backfilling tags onto historical Emails DB rows. New emails get tagged going forward; old rows stay un-tagged. Easy follow-up script if the client wants it.
- Auto-creation of the `Tags` multi-select column in Notion. The user adds it manually once (Notion → Emails DB → + → Multi-select, name "Tags") — it's a one-time UI click and we don't have a Notion API for property creation in the existing client.

## Verification

1. **User-side prep in Notion**: add a `Tags` multi-select column to the Emails database (no need to pre-fill options — Notion auto-creates them as values get written).
2. Pull the model:
   ```bash
   docker compose up -d --build
   docker compose exec api python -m gb_automations.scripts.pull_llm_model
   ```
   Wait for the pull to finish (one-time ~5GB).
3. **Smoke-test the LLM is reachable**:
   ```bash
   curl 'https://hub.tobiaseek.com/debug/llm?prompt=Hei%2C+kan+dere+sende+et+tilbud+for+leiligheten%3F'
   ```
   Expect `{"tags": ["tilbud", "spørsmål"]}` or similar — proves the chain api → ollama service → model is working.
4. **End-to-end test**: send a real email to a labeled project thread on a seeded user. Within seconds:
   - Row appears in Notion's Emails DB
   - The `Tags` column has 1-3 plausible tags from the taxonomy
   - `docker compose logs -f api` shows the tagging call happened (no warning about LLM failure)
5. **Failure-mode test**: `docker compose stop ollama`, then send another email. Confirm:
   - Row STILL appears in Notion (tagging failure doesn't break sync)
   - Tags column is empty
   - Logs show the LLM call failed but sync continued
6. **GPU verification (Windows prod only)**: `docker compose exec ollama nvidia-smi` should show the model loaded into VRAM (~5GB used) when a request is in flight.
7. Reload the schema cache (or restart api) so it picks up the new `Tags` column:
   ```bash
   curl https://hub.tobiaseek.com/debug/emails-schema | jq
   ```
   Expect `Tags` in the property_names list.
Claude’s Plan hi-please-look-at-linear-wall.md
User approved the plan
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Read .env.example
Read notion.py (lines 2-81)
Read docker-compose.yml
Edit docker-compose.yml
Added 16 lines
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Write docker-compose.gpu.yml
20 lines
# Layered override for Windows/Linux prod with NVIDIA GPU.
# Enables GPU passthrough for the Ollama container.
#
# Usage:
#   docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
#
# Requires nvidia-container-toolkit on Linux, or Docker Desktop with GPU
# support enabled (WSL2 backend) on Windows. Verify with:
#   docker compose exec ollama nvidia-smi

services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Edit config.py
Added 8 lines
Edit config.py
Added 16 lines
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Edit .env.example
Added 7 lines
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Write llm.py
116 lines
"""Async client for the local Ollama LLM service.

Surface area: short-text classification with JSON-schema-constrained output.
We keep the public surface tiny on purpose — later use cases (summarization,
draft replies, RAG retrieval) can add sibling helpers as needed.

Failures (network, timeout, malformed output) are caught here and surfaced as
an empty result. Callers treat tagging as best-effort: if the LLM is down the
sync still completes, the email row just lands without tags.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from gb_automations.config import settings

logger = logging.getLogger(__name__)


_SYSTEM_PROMPT = (
    "Du klassifiserer e-poster fra et 3D-rendering-studio (Goldbox). "
    "Velg 1–3 av de tillatte tagene som best beskriver innholdet i meldingen. "
    "Returner KUN et JSON-objekt med nøkkelen 'tags' og en liste av strenger. "
    "Bruk kun tagene fra det tillatte settet. Hvis ingen passer, returner 'annet'."
)


async def classify(prompt: str, allowed_values: list[str]) -> list[str]:
    """Pick a subset of `allowed_values` that best describes `prompt`.

    Uses Ollama's `format` parameter to constrain output to a JSON object
    matching {"tags": ["..."]} with enum values restricted to `allowed_values`.
    Returns the validated tag list, or [] on any failure.
    """
    if not prompt or not allowed_values:
        return []

    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(allowed_values)},
            }
        },
        "required": ["tags"],
    }
    body = {
        "model": settings.ollama_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Tillatte tagger: {', '.join(allowed_values)}\n\n"
                    f"E-post:\n{prompt}"
                ),
            },
        ],
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.1},
    }

    try:
        async with httpx.AsyncClient(
            base_url=settings.ollama_base_url, timeout=settings.ollama_timeout_s
        ) as client:
            response = await client.post("/api/chat", json=body)
            response.raise_for_status()
            payload = response.json()
    except Exception:
        logger.warning("LLM classify call failed", exc_info=True)
        return []

    content = ((payload.get("message") or {}).get("content") or "").strip()
    if not content:
        logger.warning("LLM returned empty content; payload=%s", _truncate(payload))
        return []

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("LLM returned non-JSON content despite format=schema: %r", content[:300])
        return []

    raw_tags = parsed.get("tags") if isinstance(parsed, dict) else None
    if not isinstance(raw_tags, list):
        logger.warning("LLM JSON missing 'tags' list: %r", content[:300])
        return []

    allowed_set = set(allowed_values)
    # Preserve order, dedupe, and drop anything outside the allowed set as a
    # final defense — `format=schema` should already enforce this server-side.
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw_tags:
        if isinstance(tag, str) and tag in allowed_set and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


def _truncate(obj: Any, limit: int = 400) -> str:
    """Compact debug repr for log lines — never blows past `limit` chars."""
    try:
        text = json.dumps(obj, ensure_ascii=False)
    except Exception:
        text = str(obj)
    return text if len(text) <= limit else text[:limit] + "…"
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Read sync_thread.py (lines 2-51)
Edit sync_thread.py
Added 1 line
Read sync_thread.py (lines 296-330)
Edit sync_thread.py
Modified
Now make _build_email_row_properties async and add the LLM tagging call.

Edit sync_thread.py
Added 11 lines
Bash Confirm all callers of _build_email_row_properties are updated
IN
grep -n "_build_email_row_properties" /Users/tobiaseek/Documents/Code/gb-automations/src/gb_automations/sync/sync_thread.py

OUT
303:    properties = await _build_email_row_properties(
359:async def _build_email_row_properties(
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Write pull_llm_model.py
71 lines
"""Pull the configured Ollama model into the local volume.

Run once after the first `docker compose up -d` so the model is available
before the api tries to call it. Idempotent — pulling an already-present
model is a fast no-op.

Usage (inside the container):
    docker compose exec api python -m gb_automations.scripts.pull_llm_model

Streams Ollama's progress lines to stdout so you can watch the ~5GB download.
Re-run after changing OLLAMA_MODEL in .env.
"""

import asyncio
import json
import sys

import httpx

from gb_automations.config import settings


async def pull() -> int:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/pull"
    body = {"name": settings.ollama_model, "stream": True}

    print(f"Pulling model {settings.ollama_model!r} from {settings.ollama_base_url} …")
    last_status = ""
    # No overall timeout — large model downloads can take many minutes.
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", url, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        print(line)
                        continue
                    status = evt.get("status") or ""
                    if "completed" in evt and "total" in evt:
                        pct = (evt["completed"] / evt["total"]) * 100 if evt["total"] else 0
                        print(f"  {status}: {pct:5.1f}%", end="\r", flush=True)
                    elif status and status != last_status:
                        print(f"  {status}")
                        last_status = status
                    if evt.get("error"):
                        print(f"\nERROR: {evt['error']}", file=sys.stderr)
                        return 1
        except httpx.HTTPError as err:
            print(f"\nFailed to reach Ollama at {url}: {err}", file=sys.stderr)
            print(
                "Hint: is the `ollama` compose service running? "
                "`docker compose ps ollama`",
                file=sys.stderr,
            )
            return 1

    print(f"\nDone. Model {settings.ollama_model!r} is ready.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(pull()))


if __name__ == "__main__":
    main()
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Edit debug.py
Added 2 lines
Edit debug.py
Added 12 lines
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Read setup.md (lines 176-235)
Edit setup.md
Added 28 lines
Edit setup.md
Added 1 line
Update Todos

Add ollama service + volume to docker-compose.yml

Create docker-compose.gpu.yml override

Add Ollama settings + EMAIL_TAGS to config.py

Update .env.example with new vars

Create clients/llm.py with classify()

Wire tagging into _build_email_row_properties

Create pull_llm_model.py script

Add /debug/llm route

Update docs/setup.md with LLM steps
Lint and smoke-import to catch any wiring mistakes.

Bash Ruff check across the package
IN
uv run ruff check src/gb_automations/ 2>&1 | tail -10

OUT
warning: `VIRTUAL_ENV=/Users/tobiaseek/.pyenv/versions/3.12.10` does not match the project environment path `.venv` and will be ignored; use `--active` to target the active environment instead
All checks passed!
Bash Smoke-import everything and print config values
IN
uv run python -c "from gb_automations.clients import llm; from gb_automations.scripts import pull_llm_model; from gb_automations.routes import debug; from gb_automations.sync import sync_thread; from gb_automations.config import EMAIL_TAGS, settings; print('imports ok; tags=', EMAIL_TAGS); print('ollama_url=', settings.ollama_base_url, 'model=', settings.ollama_model)"

OUT
warning: `VIRTUAL_ENV=/Users/tobiaseek/.pyenv/versions/3.12.10` does not match the project environment path `.venv` and will be ignored; use `--active` to target the active environment instead
imports ok; tags= ['tilbud', 'bestilling', 'korreksjon', 'leveranse', 'spørsmål', 'underlag', 'møte', 'faktura', 'intern', 'annet']
ollama_url= http://ollama:11434 model= llama3.1:8b-instruct-q4_K_M
All green. Summary of what shipped:

docker-compose.yml — new ollama service + ollama_data volume (CPU base — works everywhere).
docker-compose.gpu.yml — new GPU override file (Windows prod).
config.py — ollama_base_url, ollama_model, ollama_timeout_s settings + EMAIL_TAGS taxonomy (Norwegian terms from the brief).
.env.example — new vars documented, with the Mac-host override note.
clients/llm.py — async classify(prompt, allowed_values) using Ollama's /api/chat with JSON-schema-constrained output. Returns [] on any failure (logged), never raises.
sync_thread.py — _build_email_row_properties is now async, calls llm_client.classify() to populate the Tags multi-select. Skips the LLM call entirely if the Tags property doesn't exist in the DB schema.
scripts/pull_llm_model.py — one-shot model pull with streamed progress.
routes/debug.py — /debug/llm?prompt=… smoke endpoint.
docs/setup.md — new step 11b (pull model + GPU note + Notion Tags column step) + recovery row.
To roll out:

Add the Tags column in Notion first (Emails DB → + → Multi-select → name it Tags). Don't pre-fill options.

docker compose up -d --build
docker compose exec api python -m gb_automations.scripts.pull_llm_model
The pull is ~5GB; takes 5–10 min on a decent connection.
Smoke-test:

curl 'https://hub.tobiaseek.com/debug/llm?prompt=Hei,%20kan%20dere%20sende%20et%20tilbud%20for%20leiligheten?' | jq
Expect tags like ["tilbud", "spørsmål"]. First call may take ~30s (cold model load); subsequent calls are 1–3s on CPU, sub-second on the RTX 4080.
Refresh the schema cache so the api picks up the new column:

curl 'https://hub.tobiaseek.com/debug/emails-schema' | jq
Should list "Tags".
Send a real email on a labeled project thread → confirm the Notion row has 1–3 tags from the taxonomy.
If anything goes sideways, docker compose logs -f api will show what failed — the classify() call swallows exceptions and logs a LLM classify call failed warning rather than breaking sync.

okay nice. So. heres the thing, we are now on the macbook. But, i guess now is the time to port this over to the windows pc. becuase it wont work on my macbok m4?

How should i do that?
Quick clarification first: it does work on your M4 — Docker on Apple Silicon runs Ollama CPU-only (Mac can't pass Metal through to a Linux container), so tagging takes ~30s instead of ~1s, but sync correctness is the same. If you want fast Mac dev specifically, the workaround is to install Ollama natively on the Mac host (Metal-accelerated), then set OLLAMA_BASE_URL=http://host.docker.internal:11434 in .env. But it's not blocking.

That said — yes, now's the right time to bring up the Windows PC, both because the LLM wants GPU and because production needs to be always-on at the office, not on your laptop.

Porting flow (one-time, ~45 min):

Windows host prereqs

Install Docker Desktop for Windows with the WSL2 backend (default since 4.x).
Install NVIDIA drivers for the RTX 4080. Reboot.
In Docker Desktop → Settings → Resources → make sure "Use the WSL 2 based engine" is on. GPU passthrough is automatic once WSL2 + NVIDIA drivers are present (no separate nvidia-container-toolkit install needed on Windows — Docker Desktop bundles it).
Install Git for Windows.
Verify GPU is visible to Docker:

docker run --rm --gpus all nvidia/cuda:12.2.0-base-ubuntu22.04 nvidia-smi
Should print the 4080.
Clone the repo


cd C:\Users\<you>\Code
git clone https://github.com/cs-tobias/gb-automations.git
cd gb-automations
Copy two things from your Mac (gitignored, can't go through GitHub):

secrets/gcp-service-account.json → into Windows gb-automations\secrets\
.env → into Windows gb-automations\.env. Same Notion token, same Gmail SA, same Cloudflare tunnel token, same Pub/Sub config.
Use AirDrop, USB stick, or scp. Don't paste in chat / Slack / email.

Stop the Mac stack first before bringing Windows up, so the Cloudflare tunnel and Gmail Pub/Sub only have one consumer:


# on the Mac
docker compose down
Bring up the stack on Windows, with the GPU override:


docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
Alembic migrations run automatically on api start — you'll get a fresh Postgres on Windows. That's fine: the Notion side is the source of truth; the cache rebuilds as new emails arrive.

Run the bootstrap scripts (same as Mac, fresh DB so do them all):


docker compose exec api python -m gb_automations.scripts.seed_users tobias@goldbox.no marius@goldbox.no petter@goldbox.no
docker compose exec api python -m gb_automations.scripts.start_watches
docker compose exec api python -m gb_automations.scripts.backfill_project_labels
docker compose exec api python -m gb_automations.scripts.pull_llm_model
The model pull will be fast (gigabit) and the inference will be the fast version (~1s/email).

Verify:


curl https://hub.tobiaseek.com/health
docker compose exec ollama nvidia-smi   # confirm GPU is loaded
Three gotchas to flag:

Don't run both stacks at once. Same Cloudflare token, both will tunnel; Cloudflare round-robins → split-brain webhooks. Stop Mac, then start Windows.
Postgres data doesn't follow. The local dedup cache (email_rows, contact_cache, project_labels) starts empty on Windows. Notion still has everything — on next sync, the api re-queries Notion to verify "is this message already a row?" before creating, so no duplicates. The cache repopulates organically over a few days. If you want zero-cost zero-risk, pg_dump from Mac, pg_restore on Windows — happy to write that one-liner if you want it.
Windows file paths in .env. Anything in .env that's a host path doesn't apply — paths in there refer to inside the container (/app/secrets/gcp-service-account.json etc.), which is identical across OSes. So .env literally copies as-is.
Want me to:

(a) Add a short "Porting to the Goldbox Windows PC" section to docs/setup.md so this is durable?
(b) Write the pg_dump/pg_restore one-liner so the local cache moves over with you?
Both are quick.

okay so i dont have access to the PC yet. so lets do it here on the mac intstead for now. okay? should i just run it, cpu, or metal version?