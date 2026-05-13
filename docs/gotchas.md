# Gotchas & lessons

Things that wasted real time during setup. Re-read this before standing up the stack on a new machine or a new Workspace (e.g. Goldbox handoff).

---

## 1. `docker compose restart` does NOT reload `.env`

**Symptom:** you change a value in `.env`, run `docker compose restart api`, then exec into the container and the env var is empty or has the old value.

**Why:** Compose's `env_file:` directive is read at container **creation** time, not at restart. `restart` just stops and starts the same container instance — same env, same everything.

**Fix:**
```
docker compose up -d --force-recreate api
```
Or just `docker compose up -d` — it'll recreate any service whose config changed.

**Quick check:**
```
docker compose exec api sh -c 'echo "TOKEN length: ${#NOTION_TOKEN}"'
```

---

## 2. GCP blocks service-account JSON key creation by default

**Symptom:** in the Cloud Console, you click **Add Key → JSON** on a service account and get:
> *Service account key creation is disabled. An Organization Policy that blocks service accounts key creation has been enforced...*

**Why:** new Workspace-linked GCP orgs have "Secure by Default" enforcement of `iam.disableServiceAccountKeyCreation`. The UI shows two related constraints (`iam.disableServiceAccountKeyCreation` legacy and `iam.managed.disableServiceAccountKeyCreation` managed) — disabling only the managed one is not enough; the legacy one is what actually blocks key creation. The legacy one is often hidden from the policy list filter in newer orgs, so you can't fix it via clicks.

**Fix (via Cloud Shell — paste exactly):**

1. Find the org ID:
   ```
   gcloud organizations list
   ```
2. Grant yourself Org Policy Admin (replace `ORG_ID` and your email):
   ```
   gcloud organizations add-iam-policy-binding ORG_ID \
     --member="user:YOU@DOMAIN.com" \
     --role="roles/orgpolicy.policyAdmin"
   ```
   *(If that fails with permission denied, also grant yourself `roles/resourcemanager.organizationAdmin` first.)*
3. Override the legacy constraint:
   ```
   gcloud resource-manager org-policies disable-enforce \
     iam.disableServiceAccountKeyCreation \
     --organization=ORG_ID
   ```
4. Wait ~1 minute, retry **Add Key → JSON**.

**Verify it took:**
```
gcloud resource-manager org-policies list --project=YOUR_PROJECT
```
Both constraints should appear with the override applied.

---

## 3. Notion integration sees nothing until pages are explicitly shared with it

**Symptom:** you create a Notion internal integration and paste the token, but `/search` returns an empty array.

**Why:** Notion integrations are zero-trust by default. Having the token alone doesn't grant access to any pages — each page (or database) must be invited individually.

**Fix:** for each top-level page or database the integration needs to see:
1. Open the page in Notion
2. Click `⋯` (top-right) → **Connections**
3. Add the `gb-automations-dev` (or whatever name) integration

Children inherit access from the parent, so inviting the top-level project page is usually enough.

**Quick check:**
```
curl http://localhost:8000/debug/notion
```
Should return a non-empty `pages` array.

---

## 4. Docker Desktop install on Mac needs `sudo` *and* a GUI launch

**Symptom 1:** `brew install --cask docker` fails with `sudo: a password is required` when run from a non-interactive shell (e.g. an agent or script).
**Symptom 2:** after install, `docker info` shows only **Client** info and `failed to connect to the docker API at unix:///var/run/docker.sock`.

**Why:** the cask installer needs sudo to symlink CLI plugins into `/usr/local/cli-plugins`. After install, the daemon runs inside the Docker.app GUI process — until you launch the app, there's no daemon to talk to.

**Fix:**
```
brew install --cask docker          # run from a terminal, type your password
open -a Docker                       # or click Docker.app from /Applications
```
Wait for the whale icon in the menu bar to stop animating ("Docker Desktop is running").

OrbStack is a lighter alternative — `brew install --cask orbstack` — same `docker compose ...` commands, less RAM, no sudo prompt during install.

---

## 5. Domain-Wide Delegation scopes must match the code's scopes exactly

**Symptom:** `gmail_for(email)` raises `HttpError 401: Invalid Credentials` or `403: Insufficient Permission`, even though the service account JSON is correct and `users.watch()` works for one operation but not another.

**Why:** the OAuth scopes you authorized in **admin.google.com → Security → API controls → Domain-Wide Delegation** must be a superset of the scopes the code requests. If they don't match, Google silently issues a token that's missing the missing scope.

**Fix:** in dev we use the broadest scope, `https://mail.google.com/`, which avoids juggling. In `clients/gmail.py`:
```
SCOPES = ["https://mail.google.com/"]
```
Make sure `https://mail.google.com/` (with trailing slash) is in the DWD scopes list in admin.google.com. For prod we may want to scope down to `gmail.modify` + `gmail.metadata` + `gmail.labels` — if you do, update the constant *and* the DWD authorization to match exactly.

---

## 6. The aliases trap: same mailbox, two addresses

**Symptom:** you impersonate `tobias@workspace.com` and `post@workspace.com` and get back identical message IDs.

**Why:** Workspace lets one user have multiple aliases that all share the same physical mailbox. DWD impersonation works for both, but the inbox content is identical.

**Fix:** for any test that wants to prove "the system handles multiple distinct mailboxes," use two real Workspace **users**, not aliases. Check in **admin.google.com → Directory → Users** — separate user rows = separate mailboxes; the same user with multiple addresses listed = aliases.

---

## 7. Cloudflare wildcard `A *` records hijack tunnel subdomains

**Symptom:** you create a Cloudflare Tunnel public hostname (e.g. `hub.tobiaseek.com → api:8000`), the tunnel container shows the right ingress config, the Cloudflare DNS panel shows the `Tunnel` record — but hitting the hostname returns content from your *other* origin (Vercel in our case). Even after switching the wildcards to "DNS only", the problem can persist because of resolver caching at the edge and at remote ISPs.

**Why:** if the zone has a wildcard `A *` record (proxied OR DNS-only), it can intercept queries for unconfigured-or-just-added subdomains. With proxied wildcards, Cloudflare's edge serves them via the wildcard origin. With DNS-only wildcards, intermediate resolvers (Cloudflare's own 1.1.1.1, ISP resolvers) may return the wildcard's A records ahead of the new specific Tunnel record, especially if any resolver had cached an old answer.

**Fix that actually works: delete the wildcard `*` records entirely.** Specific records (`apex`, `www`, the Tunnel record) all keep working unchanged. Subdomains nobody has configured will return NXDOMAIN, which is what you want — caught explicitly instead of silently going to the wrong origin.

**Verify:**
```
# from the public internet (use phone on cellular if your laptop has stale DNS)
curl https://hub.YOURDOMAIN.com/health
# or, force Cloudflare anycast directly to bypass any caching layer:
curl --resolve hub.YOURDOMAIN.com:443:104.21.54.242 https://hub.YOURDOMAIN.com/health
```
A response with `server: cloudflare` + `cf-ray: ...` headers + your service's body means the path is fully working.

**Bonus: laptop/router DNS caches** can hold the old wildcard answer for hours. Test with `dig @8.8.8.8 hub.YOURDOMAIN.com +short` and your phone on cellular data to confirm the public view. External services (Notion, Google Pub/Sub) use their own infra and don't share your local cache.

---

## 8. Notion webhook payload uses `parent.type == "database"`, NOT `"database_id"`

**Symptom:** `page.created` events arrive at your webhook, signatures verify, but your filter for "is this a row in the Projects database?" rejects every event.

**Why:** Notion's REST API uses `parent.type == "database_id"` and stores the ID in `parent.database_id`. But Notion's *webhook* events use `parent.type == "database"` and store the ID in `parent.id`. They look almost identical — easy miss.

**Reference payload (real, captured from `gb-automations-dev` integration):**
```json
{
  "type": "page.created",
  "entity": { "id": "<page-id>", "type": "page" },
  "data": {
    "parent": {
      "id": "<database-id>",
      "type": "database",
      "data_source_id": "<...>"
    }
  }
}
```

**Fix:** when filtering, accept both shapes (so the same code works against API responses and webhook payloads):
```python
if parent.get("type") not in ("database", "database_id"):
    return False
parent_id = (parent.get("id") or parent.get("database_id") or "").replace("-", "")
```

**Bonus:** signature verification uses HMAC-SHA256 of the raw body with the verification token (`secret_...`) as the key. Header is `X-Notion-Signature: sha256=<hex>`. Notion's first POST is the verification handshake — the body contains `verification_token`, no signature header; you echo the token back in the response body and `X-Notion-Verification-Token` header to confirm URL ownership. Save the same token as the signing secret for all future events.

---

## 9. Application logs need to be at WARNING level to be visible in `docker compose logs`

**Symptom:** you add `logger.info("...")` to debug a webhook handler. Hit it. See uvicorn's access log line in container output but none of your application logs.

**Why:** uvicorn's default log config emits its own access logs at INFO but suppresses application loggers below WARNING in many setups. Your `gb_automations.routes.webhooks` logger isn't visible in container output.

**Fix:** for production, configure logging properly via `logging.dictConfig` or pydantic-settings → uvicorn `--log-config`. For ad-hoc debugging, just bump the level:
```python
logger.warning("debug info: %s", value)
```
WARNING+ propagates through default config. Switch back to `info` once the issue is fixed.

---

## 10. Don't pass `index=True` to `sa.Column` *and* call `op.create_index` for the same column

**Symptom:** Alembic migration fails on `CREATE INDEX ix_<table>_<col> ... already exists` even though the table didn't exist before this migration.

**Why:** `sa.Column(..., index=True)` inside `op.create_table(...)` *auto-generates* an index using Alembic's default naming convention (`ix_<table>_<col>`). If you also call `op.create_index("ix_<table>_<col>", ...)` afterwards, the explicit call collides with the auto-generated one.

**Fix:** pick one. For migration files, prefer the explicit `op.create_index(...)` call (more readable) and leave `index=True` *off* the column:
```
sa.Column("gmail_thread_id", sa.String(64), nullable=False),  # no index=True
...
op.create_index("ix_email_rows_gmail_thread_id", "email_rows", ["gmail_thread_id"])
```
The `index=True` flag in the SQLAlchemy *model* (in `models.py`) is fine — that's only honored by `Base.metadata.create_all()`, which we don't use; Alembic ignores it.

**Recovery:** Postgres uses transactional DDL with Alembic, so a failed migration rolls back fully — `alembic_version` stays at the previous head and no partial tables remain. After fixing the migration file, rebuild the api image (`docker compose up -d --build api`); the entrypoint re-runs `alembic upgrade head` cleanly.

---

## When this list grows

Add an entry whenever something costs you more than 15 minutes to figure out the second time. Future-you and the office PC handoff will both thank you.
