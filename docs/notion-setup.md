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

## Part 4 — Register the webhook

This part you do **after** `docker compose up -d --build` has the stack running and reachable at `https://hub.{your-domain}`.

1. Back to https://www.notion.so/profile/integrations → click your `gb-automations` integration
2. Open the **Webhooks** tab → **+ Create a subscription**
3. **Endpoint URL**: `https://hub.{your-domain}/webhooks/notion`
4. **Events**: tick **`page.created`** AND **`page.properties_updated`**
5. Click **Save**

Notion sends a verification POST to your endpoint. To grab the token:

```
docker compose logs api | grep "Notion webhook verification"
```

6. Copy the `secret_…` token from the log line
7. Paste it into Notion's **Verification token** field, click **Verify**
8. Paste the **same** token into `.env` as `NOTION_WEBHOOK_SECRET`
9. Reload the api: `docker compose up -d --force-recreate api`

---

Notion is done.
