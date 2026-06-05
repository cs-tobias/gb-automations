# Notion setup

All clicks, no script. About 10 minutes.

---

## Part 1 — Create the integration

1. Open https://www.notion.so/profile/integrations and sign in
2. Click **+ New integration**
3. Fill in:
   - **Name**: `gb-automations`
   - **Associated workspace**: the target workspace (e.g. Goldbox)
   - **Type**: Internal
   - **Capabilities**: Read content, Update content, Insert content, **Read user information including email addresses**
4. Click **Save**
5. Copy the **Internal Integration Secret** (starts with `ntn_…`) → paste into `.env` as `NOTION_TOKEN`

---

## Part 2 — Share the databases

The integration starts with zero access. You have to invite it to each database it needs to see.

For each of these — **Projects**, **Contacts**, and the **Emails parent page**:

1. Open the database in Notion
2. Click the **⋯** menu (top-right)
3. Click **Connections** → **Add connections**
4. Pick **gb-automations**

Children inherit access from their parent, so sharing the top-level page is usually enough.

---

## Part 3 — Add two columns to the Emails database

The sync writes to two columns that don't exist by default. Add them once:

1. Open the Emails database
2. Click **+** at the end of the column header row
3. Add **Tags** — type: **Multi-select**, leave options empty (the LLM fills them automatically)
4. Add **Files** — type: **Files & media** (gets populated with Drive links for any non-signature email attachments)

---

## Part 4 — Add the "Sync to Gmail" button

This part you do **after** `docker compose up -d --build` has the stack running and reachable at `https://hub.{your-domain}`.

Set `NOTION_WEBHOOK_SECRET` in `.env` to a long random string (e.g. `openssl rand -hex 32`) and reload: `docker compose up -d --force-recreate api`. This same value goes into the button's `Authorization` header below.

### 4a — (Upgrading from auto-webhook only) Remove the old subscription

Skip this section on a fresh install. If you previously registered an automatic webhook subscription:

1. https://www.notion.so/profile/integrations → click `gb-automations`
2. Open the **Webhooks** tab
3. Delete the existing `/webhooks/notion` subscription

### 4b — Add the button property to the Projects DB

1. Open the **Projects** database in Notion
2. Click **+** at the end of the column header row → **Button** → name it `Sync to Gmail`
3. Click the new column header → **Edit automation** → **+ Add step** → **Send webhook**
4. Fill in:
   - **URL**: `https://hub.{your-domain}/webhooks/notion`
   - **Method**: `POST`
   - **Body** (JSON):
     ```json
     {"page_id": "{{page.id}}"}
     ```
     Use Notion's variable picker for `{{page.id}}` rather than typing the braces by hand. The preview pane should show an actual UUID, not the literal string `{{page.id}}`.
   - **Headers**:
     - `Authorization` → `Bearer <paste value of NOTION_WEBHOOK_SECRET from .env>`
     - `Content-Type` → `application/json`
5. Save the automation

### 4c — Test it

1. Open any project row → click the **Sync to Gmail** button
2. Tail logs: `docker compose logs -f api | grep -v "GET /health"`
   - First click: `action: created` and one `creating Gmail label …` line per active user
   - Click again: `action: unchanged`
3. Open one mailbox in Gmail — confirm the nested label appears under `Prosjekt/<year>/`
4. Rename the project title in Notion, click the button → `action: renamed`, label renamed in every mailbox

### 4d — Onboard existing projects

For every project that pre-dates the integration, open it and click `Sync to Gmail` once. That's it — no script to run.

---

## Part 5 — Add the resync buttons (optional but recommended)

Two buttons that re-run the sync on demand — e.g. after a code change, or to repair rows that look wrong. Both just enqueue work onto the durable queue (the same queue every sync uses), then return immediately; the queue worker rebuilds the rows under current code with the normal logging/retries/status-dot. Both use the same `NOTION_WEBHOOK_SECRET` and button setup as Part 4 — only the **URL** and the **database** differ.

### 5a — "Re-sync" button on the Emails DB (per-thread)

1. Open the **Emails** database → **+** → **Button** → name it `Re-sync`
2. Header → **Edit automation** → **+ Add step** → **Send webhook**:
   - **URL**: `https://hub.{your-domain}/webhooks/notion/resync-thread`
   - **Method**: `POST`
   - **Body**: `{"page_id": "{{page.id}}"}` (use the variable picker for `{{page.id}}`)
   - **Headers**: `Authorization` → `Bearer <NOTION_WEBHOOK_SECRET>`, `Content-Type` → `application/json`

Clicking it on a row rebuilds that one thread.

### 5b — "Resync Project" button on the Projects DB (whole project)

Same as above, on the **Projects** database, named `Resync Project`, with **URL**:

```
https://hub.{your-domain}/webhooks/notion/resync-project
```

Clicking it on a project row enqueues **every** thread under that project's Gmail label(s) for a rebuild — the project-level equivalent of the per-email `Re-sync`.

### 5c — Test it

1. Click `Resync Project` on a project row. The webhook returns instantly.
2. `docker compose logs -f api | grep -v "GET /health"` — expect `🔁 resync project requested … enqueued N thread(s)`, then the worker draining each thread (`🧵 sync start / • matched 1 project(s) / 🧵 sync done`).
3. `curl http://localhost:8000/debug/queue` shows the threads moving `pending → done`.
4. Click again while it's running → already-queued threads are skipped (idempotent, no duplicates).

## Part 6 — Status-driven auto-provisioning (Projects DB)

The buttons in Parts 4–5 stay, but you can also wire up two **Notion automations** on the Projects DB so the right systems provision themselves as a project moves through its lifecycle — no clicking required.

Mapping (cumulative — a later status fires every earlier-status engine, all four are idempotent so re-runs are no-ops on already-provisioned systems):

| Status | Auto-provisions |
| --- | --- |
| `Tilbudsfase` | Gmail labels |
| `Tilbud godkjent` | Gmail + NAS folders |
| `I produksjon` | Gmail + NAS + Frame.io + Toggl |
| `Klar til oppstart`, `Venter på avklaring`, `Lang pause`, `Ferdig`, `Tapt` | No-op (recognized, do nothing) |

Each engine respects its own env toggle (`SYNC_GMAIL_LABELS`, `SYNC_NAS_FOLDERS` + `NAS_PROJECTS_ROOT`, `SYNC_FRAME`, `SYNC_TOGGL`) — a globally-off engine surfaces as `skipped` in `/debug/queue` rather than silently no-op'ing.

### 6a — Set up the automation

1. Open the **Projects** database → top-right **🗲** (Automations) → **+ New automation**
2. Name it `Auto-provision on Status change`
3. **Trigger**: `Property edited` → choose `Status`
   - This narrow trigger is what stops every other edit on a project row (Frame/NAS/Toggl URL writebacks, the `Sync` icon, `Sync progress`, **the title**, etc.) from firing this webhook. Edits inside the page body or in any embedded database also do not fire it.
   - **Do NOT add a second automation on `Name`.** An earlier draft of this doc paired the Status automation with a Name-edited one (so a rename out of the template name would mint the Gmail label without re-touching Status). In practice Notion fires Name-edited multiple times per rename (autosave + per-pause coalescing), which storms the queue with redundant `label_sync` work. The intended workflow is: duplicate template → rename → set Status — the title is real by the time Status fires, so the Name automation is unnecessary.
4. **Action**: `+ Add step` → **Send webhook**
   - **URL**: `https://hub.{your-domain}/webhooks/notion/project-status`
   - **Method**: `POST`
   - **Headers**: `Authorization` → `Bearer <NOTION_WEBHOOK_SECRET>`, `Content-Type` → `application/json`
   - **Body**: leave defaults — the receiver only reads the page id; the worker re-fetches the page at processing time.

**Why this endpoint is a sub-50ms ack and not the actual provisioning work**: Notion auto-pauses webhook automations whose receiver responds too slowly (the timeout isn't published — community reports + [n8n issue #12257](https://github.com/n8n-io/n8n/issues/12257) indicate Notion's pause heuristic is over-eager and fires on slow 200s, not just 5xx). An earlier shape did 2 Notion API calls + 5 Postgres inserts inline before responding, which on Goldbox's prod workspace was tripping the pause. The receiver now does only bearer check + parent-DB check + a single insert that enqueues a `project_status_dispatch` task, then returns 200. The queue worker drains that task and does all the actual work (Notion fetch, placeholder gate, status read, fan-out to gmail/nas/toggl/frame + per-leveranse fan-out + active/inactive lane). If the automation pauses itself on you again, that points at network/TLS latency between Notion and your Cloudflare tunnel — not the app.

### 6b — The placeholder-title gate

Goldbox creates new projects by duplicating a template row literally named `000_Kunde_Prosjekt TEMPLATE`. If a user sets Status *before* renaming, the webhook would otherwise mint Gmail labels / NAS folders / Frame projects named `000_Kunde_Prosjekt TEMPLATE`, and two new template-named rows existing at the same time would collide on a single shared Gmail label.

The receiver guards against this: when the title is still in `PROJECTS_PLACEHOLDER_TITLES` (currently just `000_Kunde_Prosjekt TEMPLATE`) the webhook skips every engine and logs a clear `skipped: placeholder title`. **Recovery path:** the user renames the row, then re-touches Status (e.g. change to a different value and back, or just re-select the same value). The next webhook fires against the real title.

The list lives in [src/gb_automations/config.py](../../src/gb_automations/config.py) under `PROJECTS_PLACEHOLDER_TITLES` — edit there if the template name changes.

### 6c — Frame.io active/inactive on terminal statuses

The same `Status`-edited automation also drives the project's **Frame.io active/inactive flag** (Frame V4 replaces the legacy "archive" with a `status: active | inactive` field on the project — fully reversible). Mapping lives in [src/gb_automations/config.py](../../src/gb_automations/config.py) `PROJECT_STATUS_INACTIVE_TRIGGERS`:

| Notion Status | Frame.io project status |
| --- | --- |
| `Ferdig`, `Tapt` | `inactive` |
| anything else (incl. empty / cleared) | `active` |

This is independent of the provisioning fan-out — it fires on *every* status change, including the previously-no-op ones (`Klar til oppstart`, `Venter på avklaring`, `Lang pause`, `Ferdig`, `Tapt`). The engine reads Frame's current status first and skips the PATCH when it already matches (`/debug/queue` shows the task as `unchanged`). If the project has no Frame entity yet (no `FrameProjectFolder` cache row), the engine no-ops silently — the next Sync Frame button click or `I produksjon` status change provisions it fresh.

**Reopening a finished project**: moving Status from `Ferdig`/`Tapt` to anything else automatically flips Frame back to `active`. No manual unarchive needed.

**One-way only**: we never read Frame's status and write it back to Notion. Notion is the source of truth for project lifecycle.

### 6d — Test it

1. Duplicate the template row (title stays `000_Kunde_Prosjekt TEMPLATE`) and set Status = `Tilbudsfase`. Expect `docker compose logs -f api` to show `project-status: page <id> title '000_Kunde_Prosjekt TEMPLATE' is a placeholder — skipping auto-provision`. `/debug/queue` shows no new tasks.
2. Rename the row to a real project name. Nothing should happen yet (no Name automation). Re-touch Status (toggle it off then back to `Tilbudsfase`). Expect `🏷  gmail-only sync requested for 'Real Name'` in the logs and a `label_sync` task on `/debug/queue` → Gmail label appears in seeded mailboxes. A `frame_project_status_sync` task also lands (it'll no-op if there's no Frame project yet — log: `no FrameProjectFolder cache row`).
3. On a fresh project (already renamed), set Status directly to `I produksjon`. Expect (env flags permitting) five queue rows: `label_sync`, `nas_folder_sync`, `toggl_project_sync`, `frame_project_sync` + N `frame_leveranse_sync`, **plus** `frame_project_status_sync`. Each engine that's globally disabled is reported as `skipped` in the JSON response.
4. Change Status to `Ferdig`. The provisioning fan-out is empty (`engines: []` in the response), but `frame_project_status_sync` is queued and the engine flips the Frame project to `inactive`. Confirm in the Frame UI: the project shows as Inactive.
5. Change Status back to `I produksjon`. Provisioning re-fires (idempotent — no-ops on already-provisioned systems), and the Frame project flips back to `active`. No manual unarchive needed.
6. Edit any other property on a project row (e.g. the Frame.io URL, or rename the row). Expect zero webhook hits — the trigger is property-scoped to `Status`.

### 6e — If the automation pauses itself

Notion auto-pauses any "Send webhook" automation that it thinks is failing. The pause indicator is an **exclamation mark next to the automation row** in the database's Automations panel. No retry, no log on Notion's side, and the published failure threshold is *undocumented* — community reports (n8n#12257, activepieces#6422) confirm even a single non-2xx response can trip it. To re-enable: open the database → top-right **🗲** → toggle the paused automation back on.

**Mitigations already in place** (see receivers in [src/gb_automations/routes/webhooks.py](../../src/gb_automations/routes/webhooks.py)):

- All three Notion-automation receivers (`/notion/project-status`, `/notion/oppgave-done`, `/notion/oppgave-status`) deliberately return **HTTP 200 for every code path** — auth failure, malformed JSON, missing page id, wrong parent DB. The failure stays loud in the api logs (`logger.warning(...)`), but Notion only ever sees a 200 and won't pause the automation.
- The endpoints are queue-based: the webhook does a single DB insert and returns. The actual work runs on the queue worker. Sub-50ms response time keeps us safely under Notion's (undocumented) latency cutoff.

**Diagnose a pause** (in order, fastest first):

1. **Is Notion firing the automation at all?** Run `curl http://localhost:8000/debug/notion-automation-health` on the api host. Each receiver records `last_seen_utc`, `last_action`, and a per-process call counter. An empty entry (or one that hasn't ticked in hours despite Status edits in Notion) means Notion isn't reaching us — skip to step 3.
2. **Is the secret in sync?** `last_action: auth_failed` repeatedly in the health endpoint means `NOTION_WEBHOOK_SECRET` in `.env` has drifted from the value Notion is sending. Re-paste the bearer in the Notion automation's webhook headers, or rotate the secret in `.env` + `docker compose up -d --force-recreate api`.
3. **Cloudflare edge filtering.** Notion's webhook IPs occasionally get caught by Cloudflare's Bot Fight Mode and are silently 403'd before reaching the origin (we see nothing in api logs because the request never arrives). Check **Cloudflare dashboard → Security → Events** for the zone, filtered to host `hub.<domain>` and path `/webhooks/notion/project-status`. If you see Block / Challenge entries against Notion's IPs, **Security → Settings → Bot Fight Mode → Off** for the zone (BFM does not run on the Ruleset Engine, so WAF Skip rules cannot whitelist Notion — the only options are off, upgrade to Super Bot Fight Mode, or expose this hostname DNS-only outside the Tunnel).
4. **Control test.** Create a free `webhook.site` URL and temporarily point the automation at it. Flip a Status; check whether (a) webhook.site receives the call, (b) the automation stays alive against webhook.site. If both, the problem is in our path (Cloudflare or origin). If the automation pauses even against webhook.site, the Notion automation itself is in a stuck state — delete and recreate it.

---

Notion is done.
