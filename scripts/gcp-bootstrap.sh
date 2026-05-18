#!/usr/bin/env bash
# GCP bootstrap for gb-automations — paste into Google Cloud Shell.
#
# Run as the Workspace super-admin (e.g. petter@goldbox.no). Cloud Shell will
# auth you to whichever organization you're signed into.
#
# What this does (≈2 min, ~10 commands):
#   1. Creates a fresh GCP project under your Workspace org
#   2. Enables Gmail / Drive / Pub/Sub / Admin APIs
#   3. Grants your own user roles/orgpolicy.policyAdmin (needed for step 4)
#   4. Overrides the two "Secure by Default" org policies that block setup
#   5. Creates the gb-automations-sync service account
#   6. Generates a JSON key (downloadable from Cloud Shell's file picker)
#   7. Creates Pub/Sub topic + subscription + IAM grants for Gmail push
#
# What this does NOT do (you do these manually after):
#   - Domain-Wide Delegation in admin.google.com  ← uses the SA Unique ID we print
#   - Cloudflare zone, tunnel, DNS                ← separate flow
#   - Notion integration + DB sharing + webhook   ← separate flow
#   - docker compose up on the host PC            ← separate flow
#
# How to use:
#   1. Open https://console.cloud.google.com signed in as petter@goldbox.no
#   2. Click the `>_` icon (top-right) → "Open in new tab" for full-screen
#   3. Edit the four INPUTS lines below
#   4. Paste the whole file into Cloud Shell, hit Enter
#   5. When it finishes, follow the printed "NEXT STEPS" block
#
# Cloud Shell times out after ~20 min idle. If it dies mid-run, just paste
# the whole script again — every gcloud call below is idempotent.

set -euo pipefail

# ────────────────────────────────────────────────────────────────────────────
# INPUTS — edit these four lines, then paste the rest unchanged.
# ────────────────────────────────────────────────────────────────────────────

DOMAIN="tobiaseek.com"
PROJECT_ID="tobiaseek-gb-test"
PROJECT_NAME="gb-automations test"

# ────────────────────────────────────────────────────────────────────────────
# Constants — don't change unless you know why.
# ────────────────────────────────────────────────────────────────────────────

SA_ID="gb-automations-sync"
SA_DISPLAY="gb-automations sync service account"
PUBSUB_TOPIC="gmail-events"
PUBSUB_SUBSCRIPTION="gmail-events-push"
HOSTNAME="hub.${DOMAIN}"
PUSH_ENDPOINT="https://${HOSTNAME}/webhooks/gmail"
APIS=(
  gmail.googleapis.com
  drive.googleapis.com
  pubsub.googleapis.com
  admin.googleapis.com
  cloudresourcemanager.googleapis.com
  iam.googleapis.com
  orgpolicy.googleapis.com
)

# ────────────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────────────

say()  { printf "\n\033[1;36m▸ %s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m⚠\033[0m  %s\n" "$*"; }

require() {
  if [[ -z "${!1:-}" ]]; then
    echo "ERROR: variable $1 is required at the top of this script." >&2
    exit 1
  fi
}

# ────────────────────────────────────────────────────────────────────────────
# Pre-flight
# ────────────────────────────────────────────────────────────────────────────

require DOMAIN
require PROJECT_ID
require PROJECT_NAME

ACCOUNT="$(gcloud config get-value account 2>/dev/null || true)"
if [[ -z "$ACCOUNT" ]]; then
  echo "ERROR: gcloud is not authed. Open Cloud Shell signed into the right account." >&2
  exit 1
fi
say "Authed as: $ACCOUNT"

# Find the org that matches DOMAIN.
say "Looking up organization for $DOMAIN"
ORG_ID="$(gcloud organizations list --format='value(ID)' \
  --filter="displayName=${DOMAIN}" | head -n1)"
if [[ -z "$ORG_ID" ]]; then
  echo "ERROR: no organization named ${DOMAIN} visible to ${ACCOUNT}." >&2
  echo "  Visible orgs:" >&2
  gcloud organizations list >&2
  exit 1
fi
ok "Org ID: $ORG_ID"

# ────────────────────────────────────────────────────────────────────────────
# 1. Create project
# ────────────────────────────────────────────────────────────────────────────

say "Step 1/7 — Create GCP project ${PROJECT_ID}"
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  ok "Project ${PROJECT_ID} already exists; reusing"
else
  gcloud projects create "$PROJECT_ID" \
    --name="$PROJECT_NAME" \
    --organization="$ORG_ID"
  ok "Created project ${PROJECT_ID}"
fi
gcloud config set project "$PROJECT_ID" >/dev/null

# ────────────────────────────────────────────────────────────────────────────
# 2. Enable APIs
# ────────────────────────────────────────────────────────────────────────────

say "Step 2/7 — Enable APIs (${#APIS[@]} APIs, ~30s)"
gcloud services enable "${APIS[@]}" --project="$PROJECT_ID"
ok "APIs enabled"

# ────────────────────────────────────────────────────────────────────────────
# 4. Self-grant orgpolicy.policyAdmin (needed for step 5)
# ────────────────────────────────────────────────────────────────────────────

say "Step 3/7 — Grant self roles/orgpolicy.policyAdmin on the org"
gcloud organizations add-iam-policy-binding "$ORG_ID" \
  --member="user:${ACCOUNT}" \
  --role="roles/orgpolicy.policyAdmin" \
  --condition=None \
  >/dev/null
ok "Granted (no-op if already held)"

# ────────────────────────────────────────────────────────────────────────────
# 5. Override "Secure by Default" org policies
#    (a) Allow JSON service-account key creation
#    (b) Allow non-domain principals (so we can grant gmail-api-push@system…)
# ────────────────────────────────────────────────────────────────────────────

say "Step 4/7 — Override two org policies at project scope"

gcloud resource-manager org-policies disable-enforce \
  iam.disableServiceAccountKeyCreation \
  --project="$PROJECT_ID" >/dev/null
ok "Disabled iam.disableServiceAccountKeyCreation"

cat > /tmp/allow-all-domains.yaml <<EOF
name: projects/${PROJECT_ID}/policies/iam.allowedPolicyMemberDomains
spec:
  rules:
  - allowAll: true
EOF
gcloud org-policies set-policy /tmp/allow-all-domains.yaml >/dev/null
ok "Set iam.allowedPolicyMemberDomains = allowAll"

rm -f /tmp/allow-all-domains.yaml

# ────────────────────────────────────────────────────────────────────────────
# 6. Service account + JSON key
# ────────────────────────────────────────────────────────────────────────────

say "Step 5/7 — Create service account ${SA_ID}"
SA_EMAIL="${SA_ID}@${PROJECT_ID}.iam.gserviceaccount.com"

if gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT_ID" >/dev/null 2>&1; then
  ok "Service account already exists; reusing"
else
  gcloud iam service-accounts create "$SA_ID" \
    --display-name="$SA_DISPLAY" \
    --project="$PROJECT_ID"
  ok "Created"
fi

SA_UNIQUE_ID="$(gcloud iam service-accounts describe "$SA_EMAIL" \
  --project="$PROJECT_ID" \
  --format='value(uniqueId)')"
ok "Unique ID: $SA_UNIQUE_ID  (needed for DWD step)"

KEY_FILE="$HOME/${PROJECT_ID}-sa-key.json"
if [[ -f "$KEY_FILE" ]]; then
  warn "Key file already exists at $KEY_FILE — skipping generation"
  warn "Delete it first if you want a fresh key: rm $KEY_FILE"
else
  gcloud iam service-accounts keys create "$KEY_FILE" \
    --iam-account="$SA_EMAIL" \
    --project="$PROJECT_ID"
  chmod 600 "$KEY_FILE"
  ok "Wrote key to $KEY_FILE"
fi

# Grant the SA tokenCreator on itself (required for Pub/Sub push auth).
gcloud iam service-accounts add-iam-policy-binding "$SA_EMAIL" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --project="$PROJECT_ID" \
  >/dev/null
ok "Granted SA tokenCreator on itself"

# ────────────────────────────────────────────────────────────────────────────
# 7. Pub/Sub topic + IAM + push subscription
# ────────────────────────────────────────────────────────────────────────────

say "Step 6/7 — Pub/Sub topic, publisher grant, push subscription"

if gcloud pubsub topics describe "$PUBSUB_TOPIC" --project="$PROJECT_ID" >/dev/null 2>&1; then
  ok "Topic ${PUBSUB_TOPIC} already exists"
else
  gcloud pubsub topics create "$PUBSUB_TOPIC" --project="$PROJECT_ID"
  ok "Created topic ${PUBSUB_TOPIC}"
fi

gcloud pubsub topics add-iam-policy-binding "$PUBSUB_TOPIC" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --project="$PROJECT_ID" \
  >/dev/null
ok "Granted gmail-api-push publisher on topic"

if gcloud pubsub subscriptions describe "$PUBSUB_SUBSCRIPTION" --project="$PROJECT_ID" >/dev/null 2>&1; then
  ok "Subscription ${PUBSUB_SUBSCRIPTION} already exists"
else
  gcloud pubsub subscriptions create "$PUBSUB_SUBSCRIPTION" \
    --topic="$PUBSUB_TOPIC" \
    --push-endpoint="$PUSH_ENDPOINT" \
    --push-auth-service-account="$SA_EMAIL" \
    --push-auth-token-audience="$PUSH_ENDPOINT" \
    --project="$PROJECT_ID"
  ok "Created push subscription → ${PUSH_ENDPOINT}"
  warn "The endpoint doesn't resolve yet — that's fine. GCP doesn't probe it"
  warn "until the first push, and by then Cloudflare's tunnel will be up."
fi

# ────────────────────────────────────────────────────────────────────────────
# 8. Print the values you need next
# ────────────────────────────────────────────────────────────────────────────

cat <<EOF


══════════════════════════════════════════════════════════════════════════════
✓ GCP setup complete for ${PROJECT_ID}
══════════════════════════════════════════════════════════════════════════════

▸ DOWNLOAD THE SERVICE ACCOUNT KEY
  In Cloud Shell, click the three-dot menu (top right) → Download File.
  File path to enter:
      ${KEY_FILE}
  Save it on your Mac as:
      gb-automations/secrets/gcp-service-account.json

▸ COPY THESE INTO .env (on your Mac)
  WORKSPACE_DOMAIN=${DOMAIN}
  INTERNAL_EMAILS_OR_DOMAINS=${DOMAIN}
  PUBSUB_TOPIC=projects/${PROJECT_ID}/topics/${PUBSUB_TOPIC}
  PUBSUB_AUDIENCE=${PUSH_ENDPOINT}
  PUBSUB_SERVICE_ACCOUNT_EMAIL=${SA_EMAIL}

▸ NEXT MANUAL STEP — Domain-Wide Delegation (admin.google.com)
  1. Open https://admin.google.com/ac/owl/domainwidedelegation
     (signed in as a super-admin of ${DOMAIN})
  2. Click "Add new"
  3. Client ID:    ${SA_UNIQUE_ID}
  4. OAuth scopes: https://mail.google.com/,https://www.googleapis.com/auth/drive
  5. Click AUTHORIZE
  Propagation usually takes <1 min, sometimes up to 5.

▸ THEN — Cloudflare, Notion, and host setup
  Continue with docs/setup-manual.md from "§5 Cloudflare" onwards.

══════════════════════════════════════════════════════════════════════════════
EOF
