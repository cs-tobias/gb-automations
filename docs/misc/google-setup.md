# Google setup

Quick checklist. Two parts: paste a script in Cloud Shell, then click through one page in admin.google.com.

---

## Part 1 — Cloud Shell

1. Open https://console.cloud.google.com and sign in as the Workspace super-admin
2. Click the **>_** icon (top-right) to open Cloud Shell
3. Open [scripts/gcp-bootstrap.sh](../scripts/gcp-bootstrap.sh), edit the four INPUTS lines at the top
4. Paste the whole script into Cloud Shell, hit Enter
5. Pick a billing account when prompted
6. Wait ~2 minutes
7. When it finishes, **download the SA key** from Cloud Shell (three-dot menu → Download File → paste the path it printed)
8. Save the key on your Mac as `gb-automations/secrets/gcp-service-account.json`

---

## Part 2 — Domain-Wide Delegation

1. Open the URL the script printed (admin.google.com/ac/owl/domainwidedelegation)
2. Click **Add new**
3. Paste the **Client ID** the script printed
4. Paste the **scopes** the script printed
5. Click **Authorize**

---

Google is done.
