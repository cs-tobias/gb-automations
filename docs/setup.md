# Setup — fresh deployment for a new workspace

Step-by-step to stand up gb-automations from scratch on a new Workspace + Notion (e.g. the Goldbox handoff, or any future workspace). Roughly **1 hour of clicks + 30 min of code**. Assume `goldbox.no` everywhere — substitute your domain where needed.

## What you need before starting

- **Workspace admin access** for the domain (admin@goldbox.no equivalent)
- A registered domain you control DNS for (`goldbox.no`)
- A Notion workspace where the integration will live
- The machine the stack will run on (your Mac for dev, the office PC for prod) with:
  - **Docker Desktop** (or OrbStack — `brew install --cask orbstack`)
  - **Git**
  - That's it. Everything else runs in containers.

## 1. GCP project + APIs *(5 min)*

1. https://console.cloud.google.com → top bar → **New Project** → name `gb-automations-prod` (or whatever). Note the project ID.
2. Sidebar → **APIs & Services → Library** → enable each:
   - **Gmail API**
   - **Cloud Pub/Sub API**

## 2. Override two "Secure by Default" org policies *(2 min in Cloud Shell)*

These will block you otherwise. Run in Cloud Shell (top-right `>_` icon in console), replacing `PROJECT_ID`:

```bash
gcloud config set project PROJECT_ID

# (a) Allow JSON service-account keys
gcloud resource-manager org-policies disable-enforce \
  iam.disableServiceAccountKeyCreation \
  --project=$(gcloud config get-value project)

# (b) Allow non-domain principals (needed to grant Gmail's system SA on Pub/Sub)
cat > /tmp/allow-domains.yaml << 'EOF'
name: projects/PROJECT_ID/policies/iam.allowedPolicyMemberDomains
spec:
  rules:
  - allowAll: true
EOF
sed -i "s/PROJECT_ID/$(gcloud config get-value project)/" /tmp/allow-domains.yaml
gcloud org-policies set-policy /tmp/allow-domains.yaml
```

> ⚠️ If you don't have permission, grant yourself first (replace `ORG_ID` from `gcloud organizations list`):
> ```
> gcloud organizations add-iam-policy-binding ORG_ID \
>   --member="user:you@goldbox.no" --role="roles/orgpolicy.policyAdmin"
> ```

## 3. Service account with domain-wide delegation *(10 min)*

1. GCP → **IAM & Admin → Service Accounts → Create**:
   - Name: `gb-automations-sync`
   - Skip optional roles, **Done**
2. Open the SA → copy the **Unique ID** (21-digit number) → also note the SA email.
3. **Keys** tab → **Add Key → JSON** → download. Move to `secrets/gcp-service-account.json` in the cloned repo (gitignored by default).
4. Open https://admin.google.com → **Security → Access and data control → API controls → Manage Domain-Wide Delegation → Add new**:
   - Client ID: paste the Unique ID from step 2
   - OAuth scopes: `https://mail.google.com/`
   - **Authorize**

> The single broad Gmail scope avoids juggling. For prod hardening later, narrow to `gmail.modify, gmail.metadata, gmail.labels` — must match `SCOPES` in `clients/gmail.py`.

## 4. Notion integration *(5 min)*

1. https://www.notion.so/profile/integrations → **New integration**:
   - Name: `gb-automations`
   - Workspace: Goldbox's Notion workspace
   - Type: Internal
   - Capabilities: Read content, Update content, Insert content, Read user information **including email addresses**
2. Save → copy the **Internal Integration Secret** (`ntn_…`).
3. In Notion, open the **Project** database → `⋯` (top-right) → **Connections** → add the integration. Repeat for the **Emails** database and **Contacts** database, and any top-level page the integration needs to see.

> Children inherit access from parents, so adding it on the top-level page is usually enough.

## 5. Cloudflare — domain + tunnel *(15 min)*

### 5a. Add domain to Cloudflare *(if not already there)*
1. https://dash.cloudflare.com → **Add a Site** → `goldbox.no` → Free plan.
2. Review imported DNS records. **Critically, confirm MX records (Workspace) are imported** before switching nameservers, or email breaks.
3. Replace nameservers at your registrar with Cloudflare's. Wait for activation email.

### 5b. ⚠️ Delete any `A *` wildcard records
After Cloudflare imports DNS, look at the records list. **Delete every `A *` wildcard record.** Proxied wildcards intercept new subdomains and route them to the wrong origin — they cost hours of debugging if left in place. Specific records for apex/www/etc remain.

### 5c. Create the tunnel
1. Cloudflare → **Zero Trust → Networks → Tunnels → Create a tunnel** → "Cloudflared" → name `gb-automations-prod`.
2. Copy the **token** (long `eyJ...` string). You don't need to run the suggested docker command — our compose file handles it.
3. **Public Hostnames → Add**:
   - Subdomain: `hub`
   - Domain: `goldbox.no`
   - Type: `HTTP`
   - URL: `api:8000`

## 6. Pub/Sub topic + push subscription *(5 min)*

1. GCP → **Pub/Sub → Topics → Create Topic**:
   - Topic ID: `gmail-events`
   - Uncheck "Add a default subscription"
2. Open the new topic → **Permissions → Grant Access**:
   - Principal: `gmail-api-push@system.gserviceaccount.com`
   - Role: `Pub/Sub Publisher`
3. **Subscriptions** tab → **Create Subscription**:
   - ID: `gmail-events-push`
   - Delivery type: **Push**
   - Endpoint: `https://hub.goldbox.no/webhooks/gmail`
   - **Enable authentication** ✓
   - Service account: the same `gb-automations-sync@…` from step 3 (accept the prompt to grant `roles/iam.serviceAccountTokenCreator` if asked)
   - Audience: leave blank

## 7. Clone the repo + configure .env *(5 min)*

```bash
git clone https://github.com/cs-tobias/gb-automations.git
cd gb-automations
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Value |
|---|---|
| `WORKSPACE_DOMAIN` | `goldbox.no` |
| `INTERNAL_EMAILS_OR_DOMAINS` | `goldbox.no` |
| `NOTION_TOKEN` | from step 4 |
| `EMAILS_DB_ID` / `CONTACTS_DB_ID` / `PROJECTS_DB_ID` | leave blank for now — we'll fetch them after starting |
| `CLOUDFLARE_TUNNEL_TOKEN` | from step 5c |
| `NOTION_WEBHOOK_SECRET` | leave blank for now |
| `PUBSUB_TOPIC` | `projects/PROJECT_ID/topics/gmail-events` |
| `PUBSUB_AUDIENCE` | `https://hub.goldbox.no/webhooks/gmail` |
| `PUBSUB_SERVICE_ACCOUNT_EMAIL` | the SA email from step 3 |

Place the service account JSON at `secrets/gcp-service-account.json`.

## 8. Bring up the stack *(2 min)*

```bash
docker compose up -d --build
```

Wait ~15 sec for Postgres to be healthy and the api to start. Verify:

```bash
curl https://hub.goldbox.no/health
# → {"status":"ok","env":"dev","db":"ok"}
```

If you get a Vercel/wrong response, check that the wildcard records are gone (step 5b) and that DNS has propagated (`dig @8.8.8.8 hub.goldbox.no +short` should return Cloudflare IPs like `104.21.x.x`).

## 9. Discover Notion DB IDs + paste into .env *(2 min)*

```bash
curl https://hub.goldbox.no/debug/databases
```

Find the IDs for `Emails`, `Contacts`, `Project`. Paste into `.env` as `EMAILS_DB_ID`, `CONTACTS_DB_ID`, `PROJECTS_DB_ID` (without dashes is fine).

**Recreate api to pick up the changes:**
```bash
docker compose up -d --force-recreate api
```

> `docker compose restart` does NOT reload `.env` — must use `up -d --force-recreate`.

## 10. Register the Notion webhook *(3 min)*

1. https://www.notion.so/profile/integrations → `gb-automations` → **Webhooks** → Add subscription:
   - Endpoint: `https://hub.goldbox.no/webhooks/notion`
   - Events: **`page.created`**
2. Notion sends a verification POST to your endpoint → log shows the token:
   ```bash
   docker compose logs api | grep "Notion webhook verification"
   ```
3. Paste the `secret_…` token into Notion's "Verification token" field → confirm.
4. Paste the **same** token into `.env` as `NOTION_WEBHOOK_SECRET`.
5. Recreate api: `docker compose up -d --force-recreate api`

## 11. Seed users + bootstrap Gmail watches *(2 min)*

Add every Workspace user that should be synced:

```bash
docker compose exec api python -m gb_automations.scripts.seed_users \
  tobias@goldbox.no marius@goldbox.no petter@goldbox.no
```

Start Gmail push notifications for them:

```bash
docker compose exec api python -m gb_automations.scripts.start_watches
```

Should print one `historyId` + `expiration` per user. APScheduler will keep watches renewed automatically.

## 12. Verify end-to-end *(3 min)*

**Notion → Gmail label flow:**
- Add a new row to Notion's Project database
- Within seconds to a few minutes (Notion's webhook delivery is variable), the label appears in every seeded user's Gmail

**Gmail → Notion email flow:**
- Label any email in a seeded user's inbox with one of the existing project labels
- Within seconds the email rows appear in Notion's Emails DB, contacts auto-extracted

```bash
# Watch the action live:
docker compose logs -f api | grep -v "GET /health"
```

---

## Updating after first deploy

```bash
git pull
docker compose up -d --build
```

Both api + cloudflared are managed by compose with `restart: unless-stopped`, so they survive reboots automatically. APScheduler renews Gmail watches every 5 days inside the api container.

## Recovery

| Problem | Fix |
|---|---|
| Gmail watches expired (>7 days outage) | `docker compose exec api python -m gb_automations.scripts.start_watches` |
| Need to backfill a missed thread | `docker compose exec api python -m gb_automations.sync.sync_one --email USER --thread THREAD_ID` |
| Container won't pick up new `.env` | `docker compose up -d --force-recreate api` (not `restart`) |
| Service account key compromised | Rotate: generate new JSON in GCP → swap `secrets/gcp-service-account.json` → restart api |

## See also

- `docs/gotchas.md` — issues this guide explicitly prevents, plus a few more
- `docs/reference/` — original Apps Script and client brief for historical context
