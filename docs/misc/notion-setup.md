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

---

Notion is done.
