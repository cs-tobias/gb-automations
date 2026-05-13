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

## When this list grows

Add an entry whenever something costs you more than 15 minutes to figure out the second time. Future-you and the office PC handoff will both thank you.
