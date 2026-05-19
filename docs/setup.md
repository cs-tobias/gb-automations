# Setup

Stand up gb-automations on a fresh Workspace + Notion + Cloudflare zone with a single command:

```bash
python -m gb_automations.scripts.setup_workspace
```

The installer is interactive and idempotent. State is persisted to `.setup_state.json` at the repo root — interrupt with Ctrl-C any time and resume by re-running the same command.

It runs **every automatable step itself** (`gcloud`, `cloudflared`, `docker compose`, Cloudflare REST) and pauses with a clipboard-ready URL at the two browser stops that genuinely cannot be scripted:

1. **Workspace Domain-Wide Delegation** (admin.google.com) — Workspace super-admin must authorize the service account's OAuth scopes. The installer pre-fills the Client ID via deep link and copies the scopes to your clipboard.
2. **Notion integration + DB Connections + webhook** (notion.so/profile/integrations) — Notion has no public API for these. The installer tells you exactly which DBs to share with the integration, then captures the verification token from the api logs automatically.

(Frame.io integration is currently being built and tested on a separate account. It will be added to this installer next week.)

## What you need before running

The installer's pre-flight (step 0) refuses to start unless every item below is satisfied. Get these in order, then run the installer.

### 1. Accounts / access

- **Workspace super-admin access** for the target domain (e.g. `admin@goldbox.no`). You don't need the password — you just need to be signed in when the browser opens for `gcloud auth login` and the DWD step.
- **Notion login** with admin rights to the target Notion workspace.
- **Cloudflare account login** for the target org (the installer creates the zone if it doesn't exist).
- **Domain registrar login** for the domain (to swap nameservers to Cloudflare once during step 10).

### 2. Tools you install yourself (the installer can't bootstrap these)

| Tool | Install (macOS) | Why we can't auto-install |
|---|---|---|
| **Docker Desktop** or OrbStack | `brew install --cask orbstack` (or Docker.app) | needs sudo + GUI launch (see [gotchas.md §4](gotchas.md)) |
| **Python ≥3.12** | already on most Macs, else `brew install python@3.12` | the installer itself is a Python script |
| **git** | preinstalled with Xcode CLT (first `git` command triggers the install) | needed to clone the repo |

That's it for manual installs. The installer auto-installs **gcloud**, **cloudflared**, and **uv** on first run if they're missing — it asks `Auto-install gcloud now? [Y/n]` for each.

### 3. Repo cloned

```bash
git clone https://github.com/cs-tobias/gb-automations.git
cd gb-automations
```

The installer is `python -m gb_automations.scripts.setup_workspace`. Run all subsequent commands from the `gb-automations/` directory (or any subdirectory — the installer walks up to find `pyproject.toml`).

### 4. One-time CLI logins (the installer prompts you to run these)

When the pre-flight detects missing auth, it offers to run these for you. Or run them yourself first:

```bash
gcloud auth login              # browser → sign in as workspace super-admin
cloudflared tunnel login       # browser → select the target Cloudflare zone
```

Sign in as the **target workspace's accounts** (e.g. Goldbox's, not your dev account). Auth persists in `~/.config/gcloud` and `~/.cloudflared/cert.pem` — re-run the `login` commands if you ever need to swap workspaces.

### 5. Cloudflare API token (one browser stop the installer can't automate)

Mint a token at https://dash.cloudflare.com/profile/api-tokens (signed into the target Cloudflare account) with these scopes:

- `Account → Cloudflare Tunnel:Edit`
- `Account → Zone:Create`
- `Zone → DNS:Edit (All zones)`

The installer prompts for this string in step 9. Save it in your password manager — re-running the installer reads it from `.setup_state.json` (gitignored), but if you ever lose that file you'll need the token again.

### Not required today

- Adobe IMS / Frame.io credentials — the Frame.io integration is out of scope for today's installer; it'll be added next week.
- Ollama installed on the host — Ollama runs as a container by default. The api auto-pulls the model on first boot. Mac devs who want native Metal acceleration can opt out — see [Mac dev: native Ollama for Metal](#mac-dev-native-ollama-for-metal).

## What the installer does, in order

| Step | What | How |
|---|---|---|
| 0 | Pre-flight: `gcloud`, `cloudflared`, `docker`, auth state | local checks |
| 1 | Prompt for domain, project ID, org, billing account | interactive |
| 2 | Grant yourself `roles/orgpolicy.policyAdmin` on the Workspace org | `gcloud` |
| 3 | Create GCP project, link billing | `gcloud` |
| 4 | Enable 7 APIs (gmail, drive, pubsub, admin, etc.) | `gcloud` |
| 5 | Override `iam.disableServiceAccountKeyCreation` + `iam.allowedPolicyMemberDomains` | `gcloud` |
| 6 | Create service account, download JSON key to `secrets/`, grant tokenCreator | `gcloud` |
| 7 | **BROWSER STOP** — admin.google.com DWD authorization | poll `users.getProfile` |
| 8 | Pub/Sub topic + publisher grant + push subscription with OIDC auth | `gcloud` |
| 9 | Create Cloudflare zone via REST | API |
| 10 | Wait for zone activation (you set nameservers at your registrar) | API poll |
| 11 | Auto-delete any `A *` wildcard records (Gotcha §7) | API |
| 12 | Create Cloudflare tunnel, route `hub.{domain}` DNS, fetch tunnel token | `cloudflared` |
| 13 | Write `.env` and `docker compose up -d --build` | shell |
| 14 | Wait for `https://hub.{domain}/health` to return 200 | HTTP poll |
| 15 | **BROWSER STOP** — Notion integration creation + DB sharing | poll `/v1/search` |
| 16 | Auto-discover Notion DB IDs (Projects, Contacts, Emails parent page); write to `.env`; recreate api | API |
| 17 | **BROWSER STOP** — Register Notion webhook; capture verification token from logs | log-tail |
| 18 | Prompt for Workspace users to seed; run `seed_users`, `start_watches`, `backfill_project_labels`, `pull_llm_model` | container exec |
| 19 | Smoke test, print summary | HTTP |

Total: typically ~15 minutes of clicks + ~5 minutes of background waiting (mostly Cloudflare zone activation and the LLM model pull).

## Platform notes

### Windows client: GPU passthrough

Bring the stack up with the GPU override layered on:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build
```

Requires Docker Desktop with the WSL2 backend and current NVIDIA drivers on the host. Verify the container sees the GPU:

```bash
docker compose exec ollama nvidia-smi
```

The api auto-pulls the LLM model on first boot — watch progress with `docker compose logs -f api`.

### Mac dev: native Ollama for Metal

In-container Ollama is CPU-only on Mac (Docker Desktop has no Metal passthrough). For dev, native Ollama with Metal is meaningfully faster:

```bash
brew install ollama
OLLAMA_HOST=0.0.0.0:11434 ollama serve   # 0.0.0.0 so the api container can reach it via host.docker.internal
```

In `.env`, point the api at the native process and stop the unused in-container service so it doesn't waste disk + idle RAM:

```bash
# in .env
OLLAMA_BASE_URL=http://host.docker.internal:11434

docker compose stop ollama
docker compose up -d --force-recreate api
```

## When something goes wrong

1. **Read [docs/gotchas.md](gotchas.md) first.** Most failures have an entry.
2. The installer is idempotent — re-run the same command, it'll resume.
3. To start completely fresh: `rm .setup_state.json .env` and re-run. The installer detects existing GCP / Cloudflare / Notion resources and adopts them rather than re-creating duplicates.
4. To bypass the installer entirely and do every step manually: [docs/setup-manual.md](setup-manual.md) is the long-form click-by-click guide that the installer automates. Useful when debugging a single broken step.

## Recovery & updates after first deploy

```bash
git pull
docker compose up -d --build
```

Both `api` and `cloudflared` are managed by compose with `restart: unless-stopped`, so they survive reboots automatically. APScheduler renews Gmail watches every 5 days inside the api container.

Recovery commands (run on the host):

| Problem | Fix |
|---|---|
| Gmail watches expired (>7 days outage) | `docker compose exec api python -m gb_automations.scripts.start_watches` |
| Need to backfill a missed thread | `docker compose exec api python -m gb_automations.sync.sync_one --email USER --thread THREAD_ID` |
| Want to re-sync from scratch | Delete the relevant Notion rows yourself, then `docker compose exec api python -m gb_automations.scripts.reset_thread`. Reapply the Gmail label to trigger a fresh sync. |
| Project rename in Notion didn't update Gmail label | `docker compose exec api python -m gb_automations.scripts.backfill_project_labels`, then rename again |
| Email tagging stopped working | `docker compose restart ollama && docker compose restart api` (api auto-pulls the model on boot if missing); if still broken, re-pull manually: `docker compose exec api python -m gb_automations.scripts.pull_llm_model` |
| Container won't pick up new `.env` | `docker compose up -d --force-recreate api` (not `restart`) |
| Service account key compromised | Rotate: generate new JSON in GCP → swap `secrets/gcp-service-account.json` → restart api |

For attachment / DWD / Drive issues, see [docs/gotchas.md](gotchas.md) §5 and the "Recovery" section of [docs/setup-manual.md](setup-manual.md).
