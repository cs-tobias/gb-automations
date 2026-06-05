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
   - **Body**: leave defaults — the receiver re-fetches the page to read live Status + title at processing time.

### 6b — The placeholder-title gate

Goldbox creates new projects by duplicating a template row literally named `000_Kunde_Prosjekt TEMPLATE`. If a user sets Status *before* renaming, the webhook would otherwise mint Gmail labels / NAS folders / Frame projects named `000_Kunde_Prosjekt TEMPLATE`, and two new template-named rows existing at the same time would collide on a single shared Gmail label.

The receiver guards against this: when the title is still in `PROJECTS_PLACEHOLDER_TITLES` (currently just `000_Kunde_Prosjekt TEMPLATE`) the webhook skips every engine and logs a clear `skipped: placeholder title`. **Recovery path:** the user renames the row, then re-touches Status (e.g. change to a different value and back, or just re-select the same value). The next webhook fires against the real title.

The list lives in [src/gb_automations/config.py](../../src/gb_automations/config.py) under `PROJECTS_PLACEHOLDER_TITLES` — edit there if the template name changes.

### 6c — Test it

1. Duplicate the template row (title stays `000_Kunde_Prosjekt TEMPLATE`) and set Status = `Tilbudsfase`. Expect `docker compose logs -f api` to show `project-status: page <id> title '000_Kunde_Prosjekt TEMPLATE' is a placeholder — skipping auto-provision`. `/debug/queue` shows no new tasks.
2. Rename the row to a real project name. Nothing should happen yet (no Name automation). Re-touch Status (toggle it off then back to `Tilbudsfase`). Expect `🏷  gmail-only sync requested for 'Real Name'` in the logs and a `label_sync` task on `/debug/queue` → Gmail label appears in seeded mailboxes.
3. On a fresh project (already renamed), set Status directly to `I produksjon`. Expect (env flags permitting) four queue rows: `label_sync`, `nas_folder_sync`, `toggl_project_sync`, `frame_project_sync` + N `frame_leveranse_sync`. Each engine that's globally disabled is reported as `skipped` in the JSON response.
4. Change Status to `Ferdig`. Expect `project-status: page <id> status='Ferdig' is not mapped to any engine — skipping`. No new queue rows.
5. Edit any other property on a project row (e.g. the Frame.io URL, or rename the row). Expect zero webhook hits — the trigger is property-scoped to `Status`.

---

Notion is done.
