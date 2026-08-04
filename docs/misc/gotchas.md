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

**Status:** legacy — applies to Notion's automatic database-event webhook subscriptions. The Projects → Gmail label sync was switched to a button-triggered webhook (see §13), so this filtering code is no longer in the repo. Kept here because anyone wiring up a new Notion auto-subscription elsewhere will hit the same shape.

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

## 9. Domain Restricted Sharing blocks adding `gmail-api-push@system.gserviceaccount.com` to a topic

**Symptom:** when granting `gmail-api-push@system.gserviceaccount.com` the `Pub/Sub Publisher` role on your `gmail-events` topic, GCP errors:
> *The 'Domain Restricted Sharing' organization policy (constraints/iam.allowedPolicyMemberDomains) is enforced. Only principals in allowed domains can be added as principals in the policy.*

**Why:** another "Secure by Default" policy. New Workspace-linked GCP orgs restrict IAM membership to the org's own domain. Google's system service account (`gmail-api-push@system.gserviceaccount.com`) is not in that allowed list, so the grant is rejected.

**Fix (Cloud Shell, project-scoped):**
```
cat > /tmp/allow-all-domains.yaml << 'EOF'
name: projects/YOUR_PROJECT_ID/policies/iam.allowedPolicyMemberDomains
spec:
  rules:
  - allowAll: true
EOF
gcloud org-policies set-policy /tmp/allow-all-domains.yaml
```
Wait ~30 sec, retry the IAM grant in the GCP UI.

**Scope note:** `allowAll: true` only applies to this project. Org-wide enforcement stays in place. If you want to be narrower, add Google's customer ID to the allowed list instead — but for a single dev project, allowAll is the simplest move.

---

## 10. Application logs need to be at WARNING level to be visible in `docker compose logs`

**Symptom:** you add `logger.info("...")` to debug a webhook handler. Hit it. See uvicorn's access log line in container output but none of your application logs.

**Why:** uvicorn's default log config emits its own access logs at INFO but suppresses application loggers below WARNING in many setups. Your `gb_automations.routes.webhooks` logger isn't visible in container output.

**Fix:** for production, configure logging properly via `logging.dictConfig` or pydantic-settings → uvicorn `--log-config`. For ad-hoc debugging, just bump the level:
```python
logger.warning("debug info: %s", value)
```
WARNING+ propagates through default config. Switch back to `info` once the issue is fixed.

---

## 11. Don't pass `index=True` to `sa.Column` *and* call `op.create_index` for the same column

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

## 12. Gmail's `labels.create` does NOT auto-create parents for nested labels

**Symptom:** you POST a label named `Prosjekt/2026/Acme` to `users.labels.create` expecting Gmail to render it as a nested label in the sidebar. Instead, Gmail creates a single flat label whose literal name is `Prosjekt/2026/Acme` — the `/` characters show up as text, not as hierarchy separators.

**Why:** Gmail uses `/` as the sidebar nesting separator at *render* time only. The `name` field on the Label resource is treated as an opaque string by the API. For Gmail to render `Prosjekt/2026/Acme` as nested, each prefix (`Prosjekt`, then `Prosjekt/2026`) must already exist as its own label first. If any prefix is missing, Gmail just shows the slashes literally.

**Fix:** before creating a leaf, walk the path top-down and pre-create each missing prefix. See [src/gb_automations/clients/gmail.py](../src/gb_automations/clients/gmail.py) `create_label` — it splits on `/`, snapshots the label list once, and calls `labels.create` for each prefix that isn't already present. Treat `409 Conflict` as success (concurrent webhook race).

**Same trap on rename:** `labels.patch` has the identical behavior. If you rename a label to a new nested path whose parents don't exist, you get a flat label with literal slashes. `update_label_name` pre-creates the parent of the new name before patching.

**Sources:** [labnol "Create Nested Labels in Gmail"](https://www.labnol.org/code/19895-create-nested-gmail-labels), [GAMADV-XTD3 wiki](https://github.com/taers232c/GAMADV-XTD3/wiki/Users-Gmail-Labels) (note the `buildpath` flag — it exists *because* the bare API doesn't do this).

---

## 13. Notion button webhooks are NOT HMAC-signed — auth has to be a custom header

**Symptom:** you build a `/webhooks/notion` handler that verifies `X-Notion-Signature` (as you did for the database-event subscription) and start getting 401s when the Notion **Button** "Send webhook" action calls it. There's no signature header in the request.

**Why:** Notion's two webhook surfaces are completely different products. The integration's **Webhooks** subscription (for `page.created` etc.) HMAC-signs the body with the verification token (`X-Notion-Signature: sha256=<hex>`). The Button's **Send webhook** automation has no signing — it just sends whatever body and headers you typed into the button config. Notion's docs don't put these side-by-side, so it's easy to assume parity.

**Fix:** authenticate the button via a custom header instead. We use `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` — the same env var that used to be the HMAC signing key — and compare with `hmac.compare_digest`. See [src/gb_automations/routes/webhooks.py](../src/gb_automations/routes/webhooks.py) `_verify_bearer`.

**Variable-substitution gotcha:** the button's body field supports Notion variables (`{{page.id}}`) but you have to insert them via the variable picker in the UI, not type the braces by hand. Always verify in the body preview pane that the rendered value is a real UUID — if you see the literal string `{{page.id}}` arriving at the handler, the substitution didn't take.

---

## 14. Gmail's `users.watch()` filter can't be tightened to `Prosjekt/*` — filtering happens in our code

**Symptom:** you'd expect Gmail to only push us activity on threads tagged with a `Prosjekt/*` label, but the watch is configured with `labelIds: ["INBOX", "SENT"]` and we get pushes for *every* inbox change (promo mail, UNREAD toggles, CATEGORY_UPDATES). Looks wrong at first glance.

**Why two upstream options don't work:**

1. *Pass project label IDs to `users.watch()`.* Gmail documents a ~50-label cap per watch; Goldbox does ~200 projects/year. Even rotating year-buckets blows past it eventually, and re-registering the watch on every project create adds operational fragility.

2. *Watch the parent label (`Prosjekt` or `Prosjekt/2026`).* Gmail's hierarchy is a UI convention — every label is flat in the API. Verified live: a thread tagged `Prosjekt/2026/Acme` carries only that leaf in its message `labelIds`, never the parent. `threads.list(labelIds=['<Prosjekt/2026 ID>'])` returns zero results even when child-labeled threads exist. So a parent-label watch would receive *nothing*.

Gmail filter rules don't save us either — they only run on incoming messages, not when the team manually files an existing email into a project.

**Fix (the design we shipped):** keep the coarse `INBOX/SENT` watch and filter in `_gmail_webhook_impl` immediately after `history.list`, using the `project_labels` table (one DB query) to decide whether the push touches a label we care about. Non-project pushes silently advance the cursor and return — zero Gmail/Notion calls, zero `sync_thread` invocation, one debug-level log line. From a docker-logs perspective, irrelevant pushes become invisible.

**Side effect to expect:** Pub/Sub message volume is unchanged — Gmail still pushes everything in INBOX/SENT. What changes is what *our app does* with those pushes. If you ever need to debug a push that should have synced but didn't, set the log level to DEBUG and look for `"Gmail push ignored for ..."` lines.

---

## 15. NAS project folders work locally but the container can't write to the mounted `W:` share

**Symptom:** project-folder creation works fine in dev (a local folder), but on the office host every project click reports `nas:failed` — or you see the "NAS root … is not a writable directory" warning at startup — even though Windows Explorer can read/write `W:` fine.

**Why this is its own gotcha (and not just a setup mistake):** the original design bind-mounted the Windows-side share into the container (`${NAS_HOST_PATH}:/mnt/nas`). That **does not work on Docker Desktop with the WSL2 backend** — neither a mapped drive letter (`W:`) nor a UNC path (`\\srvr\share`) in a bind survives the Windows→WSL2 VM hop. Symptoms vary: compose either refuses the path (`is not a valid Windows path`), or accepts it and binds an empty directory inside the docker-desktop VM, so files seem to write but never reach the NAS ([docker/for-win#6307](https://github.com/docker/for-win/issues/6307), still open as of 2026). `New-SmbGlobalMapping` has the same broken behavior. The only path that works on WSL2 is having Docker mount the share itself as a CIFS volume — which is what we ship now.

**Current shape (compose mounts the share, not the host):** `docker-compose.yml` declares a `nas` named volume with `driver: local`, `type: cifs`, and `NAS_CIFS_DEVICE` / `NAS_USER` / `NAS_PASS` env vars. The volume is mounted into the api container at `/mnt/nas` and `NAS_PROJECTS_ROOT=/mnt/nas/Prosjekt`. `NAS_HOST_PATH` is no longer a mount source — it's just the Windows display path written back into Notion's NAS URL column. The remaining failure modes are:

1. *CIFS mount itself failing.* If `NAS_CIFS_DEVICE` is wrong, credentials are wrong, or `cifs-utils` is missing from the `docker-desktop` WSL distro, the api **container fails to start** with `mount error(13)/(2)/(115)` in the logs (not just a warning — it never reaches uvicorn startup). Fix the device path / creds / `wsl -d docker-desktop apk add cifs-utils`.

2. *CIFS uid/gid ownership.* A CIFS mount maps all files to a single `uid`/`gid` fixed at mount time. The compose options pin `uid=999,gid=999` to match the container's `app` user (see Dockerfile). If you change the container user, update the mount options to match — otherwise `os.access(root, W_OK)` returns False, `nas_available()` reports the share unwritable, and every click reports `nas:failed` even though the mount itself succeeded.

3. *Stale volume options.* Docker caches the CIFS mount options on the volume; changing `NAS_CIFS_DEVICE` or `NAS_USER` in `.env` and re-running `docker compose up -d` won't pick them up. You must `docker compose down && docker volume rm <project>_nas && docker compose up -d` for new options to take effect.

**Fix:** the startup line `NAS project root … is mounted and writable` in the api logs is the green light. If the warning is there, the mount succeeded but is read-only (uid issue) — fix the mount options. If the api won't start at all, the CIFS mount itself failed — read the mount error. Full setup in [docs/misc/nas-setup.md](nas-setup.md).

---

## 16. A labeled thread doesn't show in Notion right away — and where the queue lives

**Symptom:** you drag 50 threads into a project label and Notion stays empty for a while; or you wonder where a "pending" sync actually lives if Docker restarts.

**Why (by design):** the Gmail webhook no longer syncs inline. It writes one durable row per thread to the Postgres `sync_tasks` table (in the *same transaction* as the history-cursor advance) and returns immediately. A single background worker (`jobs/queue_worker.py`, started in `main.py` lifespan) drains the queue **one thread at a time** — each `sync_thread` is ~30–45s because of the Ollama signature LLM, so 50 threads take ~30 min to fully land. That's expected throughput, not a failure.

**Key facts:**
- The queue is **rows on disk** (the `db_data` Postgres volume, a separate container), not in-memory. A crash/restart loses nothing: `pending` rows are still there, and a row left `in_progress` by a dead process is flipped back to `pending` on boot (`reset_in_progress()` in lifespan). The interrupted thread re-runs — safe, because `sync_thread` is idempotent (Notion-backed dedup only appends new messages).
- A reply on an already-`done` thread **re-enqueues that thread id**; the sync appends just the new message(s). Multiple replies while it's already queued collapse into the one pending row (partial unique index `uq_sync_tasks_active_thread`).
- Threads labeled while the app was **completely down** are never enqueued by a push. The boot reconcile (`enqueue_missing_for_all_projects()`) enumerates every thread under every label and enqueues anything lacking a `done`/`failed` row — the only safety net the live queue can't provide itself. It runs **on boot only** (no cron).
- After 5 failed attempts a thread parks as `failed` (visible, not retried forever) and — if `SYNC_QUEUE_DB_ID` is set — shows as 🔴 Failed in the Notion "Sync Queue" mirror.

**How to inspect:** `GET /debug/queue` is the authoritative state — counts by status, oldest-pending age, in-progress task(s), recent failed rows. The Notion "Sync Queue" DB is a best-effort live mirror on top of it (a Notion outage never blocks a sync; Postgres stays correct).

**`list_history` truncation:** Gmail's `history.list` caps at 100 records/page. A bulk label produces many records; `list_history` now follows `nextPageToken` (capped at `max_pages`). If you ever see the "hit max_pages" warning, run reconcile to backfill — pathological case only.

---

## 17. Notion button webhooks time out — never do slow work inline in a button handler

**Symptom:** the "Sync to Gmail" button (Projects DB) showed **"failed to execute"** maybe half the time or more, while the "Re-sync" and "Resync Project" buttons were rock-solid. Re-clicking sometimes worked, sometimes the button looked stuck — because Notion **auto-pauses an automation that fails**, so a timed-out click could leave it disabled until re-clicked.

**Why:** Notion's button "Send webhook" action waits only a short, undocumented window for the HTTP response and reports "failed to execute" on timeout. The original "Sync to Gmail" handler did **all** the Gmail work *inline* before responding: for every active mailbox, reconcile the label (a `labels.get`, maybe a `patch`) **and** create it (`labels.list` + up to 3 `labels.create` to walk `Prosjekt/<year>/<name>`) — sequentially. With several mailboxes that's many seconds of wall-clock, which crossed the timeout intermittently (Gmail latency varies, so it failed *some* of the time, not always). The two resync buttons never failed for one reason: they only **enqueue** onto the durable queue and return in well under a second.

Note this is NOT a Gmail rate-limit problem — per-mailbox errors were always caught and the handler still returned 200 with a "FAILED in N mailbox(es)" note. The failure was the *request never completing in time*.

**Fix (shipped):** the button now does the same thing the resync buttons do — validate fast (auth, fetch page, parent/title check), `enqueue_label_sync(page_id)`, `wake()`, return `{"action": "queued"}`. The slow work runs in the queue worker as a **`label_sync` task** (a second `task_type` on `sync_tasks` alongside `'thread'`), with retries/backoff/`/debug/queue` visibility and the Projects-DB status dot — engine in [src/gb_automations/sync/sync_labels.py](../src/gb_automations/sync/sync_labels.py). One active label_sync per project (partial unique index `uq_sync_tasks_active_label`), so fast/repeat clicks on the same project collapse to one task. **Rule of thumb: a Notion button handler must enqueue-and-return, never block on external API work.**

---

## 18. A sender's signature logo keeps re-uploading on every new thread

**Symptom:** the same client logo / headshot / corporate banner appears as a separate "attachment" on every thread from a particular sender, cluttering both Drive and Notion `Vedlegg`. The structural inline-signature rule (`Content-ID` + `<img src="cid:...">`) doesn't catch it because that sender's MUA attached the logo as plain `Content-Disposition: attachment` with no cid reference.

**Why (by design):** structural shape alone can't tell a no-cid logo from a real attached image without false positives. The system instead learns per-contact: `contact_signature_images` keys `(sender_email, content_sha1)` and bumps a counter once per distinct Gmail thread that carries those exact bytes. Past `settings.signature_learn_threshold` (default 3) the row flips to `status='signature'` and future emails skip those bytes. Re-carries within the same thread don't inflate the count (`last_thread_id` is the dedup guard). So the first 3 threads from a new sender still carry their logo; from the 4th onward it's gone.

**Inspect:**
```
Invoke-RestMethod 'http://localhost:8000/debug/signatures?status=signature'
Invoke-RestMethod 'http://localhost:8000/debug/signatures?sender=anne@example.com'
```

**Force-mark something as a signature immediately** (e.g. a known logo seen in only 2 threads, but you don't want to wait for the 3rd):
```
docker compose exec db psql -U gb -d gb -c \
  "UPDATE contact_signature_images SET status='signature' WHERE sender_email='anne@example.com' AND content_sha1='<sha1-from-/debug/signatures>';"
```

**Un-learn a wrongly-marked file** (e.g. an actual recurring deliverable byte-identical across threads):
```
docker compose exec db psql -U gb -d gb -c \
  "UPDATE contact_signature_images SET status='allowlisted' WHERE content_sha1='<sha1>';"
```
`allowlisted` never skips and never bumps; the bytes upload normally going forward.

**Cosmetic caveat:** the email body keeps its `[image: logo.png]` text reference even when the image is skipped — the body cleaning runs before the upload loop knows it's a signature. Functionally fine, just visually a stub. A future refactor could pre-hash images before row build to strip the marker too; out of scope for now.

**The threshold is per-image-bytes, not per-sender:** a sender who switches logos starts a new counter on the new bytes. A real recurring photo (different bytes each send) never accumulates and never gets dropped. A truly byte-identical recurring real attachment across 3+ threads is rare enough to allowlist by hand.

**Per-thread, not per-message:** 8 replies in one thread carrying the same logo = +1, not +8. The signal is "appeared in N independent conversations", not "seen N times".

---

## 19. Fiken graduation cron re-enqueue storm — an engine-written resting `Faktura status` must be terminal

**Symptom:** the queue fills with hundreds of `graduate_faktura` tasks every hour, each logging `scanned=1136 matched=0 skipped_already=0 skipped_no_match=1221` and doing nothing. Looks alarming ("massive invoice thing!") but writes zero — every side-effect in `graduate_project` lives inside `_reconcile_one`, which is only reached on a match, so `matched=0` means no Notion/Fiken changes. It's pure noise, but it hammers the Fiken API (full invoice catalogue re-scanned per project per hour) and starves the single worker.

**Why:** the hourly poller `_enqueue_fiken_graduations_for_all_active_projects` ([jobs/scheduler.py](../../src/gb_automations/jobs/scheduler.py)) enqueues a task for every project whose `Faktura status` is NOT in its `terminal` set. `Oppstart fakturert` (`FAKTURA_STATUS_50`) is an **engine-written resting state** — the 50% invoice already graduated and stamped `sent_at`, so the project sits there (possibly for weeks) until the operator manually picks `Til avslutningsfaktura`. If that value is missing from `terminal`, EVERY 50%-billed project is non-terminal forever and gets re-enqueued every hour indefinitely. With ~180 such projects that's ~180 pointless full-catalogue scans per hour.

**Fix / rule:** only OPERATOR-INTENT statuses (`Til …`) belong in the actively-polled (non-terminal) set. Any status the ENGINE writes as a resting end-of-step state (`Oppstart fakturert`, `Fakturert`, `Kreditert`) must be in `terminal`. When you add a new intermediate Faktura status later, decide which side wrote it: engine-resting → terminal; operator-intent → non-terminal.

**Recovery when it's already storming:** deploy the corrected `terminal` set FIRST (so the next :07 cron tick can't refill), then clear the backlog — `DELETE FROM sync_tasks WHERE task_type='graduate_faktura' AND status IN ('pending','in_progress')` (scope strictly to `graduate_faktura`; leave Gmail/label/Frame tasks alone). Confirm via `/debug/queue`. As a belt-and-suspenders kill switch, `SYNC_FIKEN_GRADUATIONS=false` + `--force-recreate api` stops the cron entirely.

---

## When this list grows

Add an entry whenever something costs you more than 15 minutes to figure out the second time. Future-you and the office PC handoff will both thank you.
