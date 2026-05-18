"""Interactive installer for gb-automations.

One command, end-to-end deployment of the Gmail + Notion stack onto a fresh
Google Workspace + Cloudflare zone + Notion workspace. Most steps run on the
host (gcloud, cloudflared, docker compose, curl). Two steps unavoidably
need the operator to click in a browser (Google Domain-Wide Delegation,
Notion integration creation) — for those the installer prepares everything,
opens the right URL, copies the right payload to the clipboard, and polls
the API to detect when the operator finished.

State is persisted to `.setup_state.json` at repo root. Re-running the
installer resumes from the last completed step (idempotent — every step
first checks whether its resource already exists).

Frame.io bootstrap is intentionally out of scope today; we'll extend this
installer with a `step_20_frame` next week.

Usage:

    python -m gb_automations.scripts.setup_workspace

Prerequisites on the host (the installer checks and tells you if missing):
  - gcloud (brew install --cask google-cloud-sdk) + `gcloud auth login`
  - cloudflared (brew install cloudflared) + `cloudflared tunnel login`
  - docker / docker compose
  - One Cloudflare API token with Account → Zone:Create + Zone → DNS:Edit
    (minted manually at dash.cloudflare.com/profile/api-tokens — the one
    Cloudflare browser stop)
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from gb_automations.scripts._installer import (
    bootstrap,
    cloudflare,
    env,
    gcp,
    notion,
    prompts,
    state,
)

# ---------------------------------------------------------------------------- #
# Constants — names of resources the installer creates. Changing one of these
# means existing deployments would no longer resolve their resources, so prefer
# adding new constants over editing.
# ---------------------------------------------------------------------------- #

SA_ID = "gb-automations-sync"
SA_DISPLAY_NAME = "gb-automations sync service account"
DWD_SCOPES = "https://mail.google.com/,https://www.googleapis.com/auth/drive"
PUBSUB_TOPIC = "gmail-events"
PUBSUB_SUBSCRIPTION = "gmail-events-push"
TUNNEL_NAME = "gb-automations-prod"
HOSTNAME_PREFIX = "hub"  # → hub.{domain}
APIS_TO_ENABLE = [
    "gmail.googleapis.com",
    "drive.googleapis.com",
    "pubsub.googleapis.com",
    "admin.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "iam.googleapis.com",
    "orgpolicy.googleapis.com",
]

TOTAL_STEPS = 19


# ---------------------------------------------------------------------------- #
# Repo discovery — installer can be invoked from any cwd.
# ---------------------------------------------------------------------------- #

def _repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "pyproject.toml").exists() and (parent / "docker-compose.yml").exists():
            return parent
    prompts.die("Could not locate repo root (no pyproject.toml + docker-compose.yml found).")
    raise AssertionError  # unreachable; satisfies type checker


# ---------------------------------------------------------------------------- #
# Step 0 — pre-flight
# ---------------------------------------------------------------------------- #

def step_00_preflight() -> None:
    prompts.step(0, TOTAL_STEPS, "Pre-flight checks")

    # Docker — must be present + daemon running. We don't auto-install this:
    # Docker Desktop / OrbStack need sudo and a GUI launch (see gotchas.md §4).
    if not shutil.which("docker"):
        prompts.die(
            "docker not found on PATH. Install Docker Desktop or OrbStack first:\n"
            "    brew install --cask orbstack\n"
            "  Then start it and re-run the installer."
        )
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            capture_output=True,
            timeout=10,
        )
        prompts.ok("docker daemon is running")
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        prompts.die("Docker daemon not responding. Start Docker Desktop or OrbStack.")

    # The three auto-installable tools — each offers to install if missing.
    if not bootstrap.ensure_tool("gcloud", auto_install=True, install_fn=bootstrap.install_gcloud):
        prompts.die("Cannot proceed without gcloud.")
    if not bootstrap.ensure_tool(
        "cloudflared", auto_install=True, install_fn=bootstrap.install_cloudflared
    ):
        prompts.die("Cannot proceed without cloudflared.")

    # uv is needed only if the operator hasn't already run `uv sync`. We
    # got here via `python -m`, so a working Python env exists — but a future
    # `uv run` invocation would still benefit from having uv installed.
    bootstrap.ensure_tool("uv", auto_install=True, install_fn=bootstrap.install_uv)

    # pbcopy (macOS) is optional — clipboard auto-copy degrades to manual.
    if shutil.which("pbcopy"):
        prompts.ok("pbcopy found (clipboard auto-copy enabled)")
    else:
        prompts.warn(
            "pbcopy not found — clipboard auto-copy will be skipped "
            "(manual copy still works)"
        )

    # gcloud auth — must be done by the operator interactively.
    try:
        account = gcp.check_authed()
        prompts.ok(f"gcloud authed as {account}")
    except gcp.GcpError:
        prompts.warn("gcloud not authenticated yet.")
        if prompts.confirm("Run `gcloud auth login` now? (opens browser)", default=True):
            subprocess.run(["gcloud", "auth", "login"], check=True)
            account = gcp.check_authed()
            prompts.ok(f"gcloud authed as {account}")
        else:
            prompts.die("gcloud auth required before continuing.")

    # cloudflared auth — same pattern.
    try:
        cloudflare.check_cloudflared_authed()
        prompts.ok("cloudflared has cert.pem (tunnel login complete)")
    except cloudflare.CloudflareError:
        prompts.warn("cloudflared not authenticated yet.")
        if prompts.confirm("Run `cloudflared tunnel login` now? (opens browser)", default=True):
            subprocess.run(["cloudflared", "tunnel", "login"], check=True)
            cloudflare.check_cloudflared_authed()
            prompts.ok("cloudflared authenticated")
        else:
            prompts.die("cloudflared auth required before continuing.")


# ---------------------------------------------------------------------------- #
# Step 1 — collect inputs
# ---------------------------------------------------------------------------- #

def step_01_collect_inputs(s: dict[str, Any]) -> dict[str, Any]:
    prompts.step(1, TOTAL_STEPS, "Workspace, project, billing, and zone inputs")

    s.setdefault("domain", prompts.ask("Workspace domain (e.g. goldbox.no)"))
    s.setdefault("project_id", prompts.ask(
        "GCP project ID to create (lowercase, hyphens ok)",
        default=f"{s['domain'].split('.')[0]}-gb-automations",
    ))
    s.setdefault("project_name", prompts.ask(
        "GCP project display name",
        default="gb-automations",
    ))

    if "org_id" not in s:
        orgs = gcp.list_organizations()
        match = next(
            (o for o in orgs if o.get("displayName", "").lower() == s["domain"].lower()),
            None,
        )
        if match:
            s["org_id"] = match["name"].split("/")[-1]
            prompts.ok(f"Found Workspace org for {s['domain']}: {s['org_id']}")
        else:
            prompts.warn(f"No org named {s['domain']} found via gcloud organizations list.")
            prompts.info("Orgs visible to your gcloud account:")
            for o in orgs:
                prompts.info(f"  {o.get('displayName')} → {o['name']}")
            s["org_id"] = prompts.ask("Enter org ID manually (numeric)")

    if "billing_account_id" not in s:
        accts = gcp.list_billing_accounts()
        open_accts = [a for a in accts if a.get("open")]
        if len(open_accts) == 1:
            s["billing_account_id"] = open_accts[0]["name"].split("/")[-1]
            prompts.ok(f"Auto-selected billing account: {s['billing_account_id']}")
        else:
            prompts.info("Available open billing accounts:")
            for a in open_accts:
                prompts.info(f"  {a['name'].split('/')[-1]}  — {a.get('displayName')}")
            s["billing_account_id"] = prompts.ask("Enter billing account ID")

    state.save(_repo_root(), s)
    return s


# ---------------------------------------------------------------------------- #
# Step 2 — grant self orgpolicy.policyAdmin
# ---------------------------------------------------------------------------- #

def step_02_orgpolicy_admin(s: dict[str, Any]) -> None:
    prompts.step(2, TOTAL_STEPS, "Grant orgpolicy.policyAdmin to your account")
    account = gcp.check_authed()
    gcp.grant_org_role(
        org_id=s["org_id"],
        member=f"user:{account}",
        role="roles/orgpolicy.policyAdmin",
    )


# ---------------------------------------------------------------------------- #
# Step 3 — create project, link billing
# ---------------------------------------------------------------------------- #

def step_03_project(s: dict[str, Any]) -> None:
    prompts.step(3, TOTAL_STEPS, "Create GCP project + link billing")
    gcp.create_project(s["project_id"], s["project_name"], s["org_id"])
    gcp.link_billing(s["project_id"], s["billing_account_id"])


# ---------------------------------------------------------------------------- #
# Step 4 — enable APIs
# ---------------------------------------------------------------------------- #

def step_04_apis(s: dict[str, Any]) -> None:
    prompts.step(4, TOTAL_STEPS, f"Enable {len(APIS_TO_ENABLE)} APIs")
    gcp.enable_services(s["project_id"], APIS_TO_ENABLE)


# ---------------------------------------------------------------------------- #
# Step 5 — disable the two "Secure by Default" org policies
# ---------------------------------------------------------------------------- #

def step_05_org_policies(s: dict[str, Any]) -> None:
    prompts.step(5, TOTAL_STEPS, "Override two org policies (key creation + member domains)")
    gcp.disable_org_policy(s["project_id"], "iam.disableServiceAccountKeyCreation")
    with tempfile.TemporaryDirectory() as tmp:
        gcp.set_org_policy_allow_all(
            s["project_id"],
            "iam.allowedPolicyMemberDomains",
            Path(tmp),
        )


# ---------------------------------------------------------------------------- #
# Step 6 — service account + JSON key
# ---------------------------------------------------------------------------- #

def step_06_service_account(s: dict[str, Any]) -> None:
    prompts.step(6, TOTAL_STEPS, "Create service account + download JSON key")
    unique_id = gcp.create_service_account(s["project_id"], SA_ID, SA_DISPLAY_NAME)
    s["sa_unique_id"] = unique_id
    s["sa_email"] = gcp.service_account_email(s["project_id"], SA_ID)

    key_path = _repo_root() / "secrets" / "gcp-service-account.json"
    gcp.create_sa_key(s["project_id"], SA_ID, key_path)

    gcp.grant_token_creator(s["project_id"], s["sa_email"])

    state.save(_repo_root(), s)


# ---------------------------------------------------------------------------- #
# Step 7 — DWD (browser stop #1)
# ---------------------------------------------------------------------------- #

def step_07_dwd(s: dict[str, Any]) -> None:
    prompts.step(7, TOTAL_STEPS, "Authorize Domain-Wide Delegation (browser stop)")

    if s.get("dwd_confirmed"):
        prompts.ok("DWD already confirmed in prior run")
        return

    unique_id = s["sa_unique_id"]
    dwd_url = (
        f"https://admin.google.com/ac/owl/domainwidedelegation"
        f"?clientIdToAdd={unique_id}&overwriteClientId=true"
    )

    prompts.browser_stop(
        title="Workspace Domain-Wide Delegation",
        url=dwd_url,
        instructions=[
            f"Sign in as a super-admin of {s['domain']}.",
            "The 'Client ID' field is pre-filled with the service account.",
            "Paste the OAuth scopes (already on your clipboard) into the scopes field.",
            "Click AUTHORIZE.",
        ],
        clipboard=DWD_SCOPES,
    )

    prompts.info("After authorizing, propagation can take up to 5 minutes.")
    prompts.press_enter("When you've clicked AUTHORIZE in admin.google.com")

    seed = prompts.ask(
        f"Email address of a real {s['domain']} user to probe DWD against",
        default=f"admin@{s['domain']}",
    )

    def probe() -> bool:
        return _probe_dwd(_repo_root() / "secrets" / "gcp-service-account.json", seed)

    if not prompts.wait_for(
        "DWD impersonation",
        probe,
        interval_s=10.0,
        timeout_s=600.0,
    ):
        prompts.die("DWD verification timed out. Double-check the scopes you pasted.")

    s["dwd_confirmed"] = True
    state.save(_repo_root(), s)


def _probe_dwd(key_path: Path, user_email: str) -> bool:
    """Attempt to impersonate `user_email` via DWD and call `users.getProfile`.

    Imports Google libs lazily so the installer's pre-flight doesn't pay the
    cost for users who never reach this step. Requires the project's uv env
    OR a pip-installed google-auth + google-api-python-client on the host.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError:
        # Fall back to gcloud-based JWT minting if the libs aren't on the host
        # Python. For simplicity we just print a manual-test instruction.
        prompts.warn(
            "google-auth not available on host Python. Install it once with:\n"
            "    pip install google-api-python-client google-auth"
        )
        return False

    if not key_path.exists():
        return False

    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path),
            scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        ).with_subject(user_email)
        svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        svc.users().getProfile(userId="me").execute()
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------- #
# Step 8 — Pub/Sub topic + IAM + push subscription
# ---------------------------------------------------------------------------- #

def step_08_pubsub(s: dict[str, Any]) -> None:
    prompts.step(8, TOTAL_STEPS, "Pub/Sub topic + push subscription")

    gcp.create_pubsub_topic(s["project_id"], PUBSUB_TOPIC)
    gcp.grant_topic_publisher(
        s["project_id"],
        PUBSUB_TOPIC,
        "serviceAccount:gmail-api-push@system.gserviceaccount.com",
    )

    hostname = f"{HOSTNAME_PREFIX}.{s['domain']}"
    push_endpoint = f"https://{hostname}/webhooks/gmail"
    gcp.create_push_subscription(
        s["project_id"],
        PUBSUB_SUBSCRIPTION,
        PUBSUB_TOPIC,
        push_endpoint,
        s["sa_email"],
        audience=push_endpoint,
    )

    s["hostname"] = hostname
    s["pubsub_topic_full"] = f"projects/{s['project_id']}/topics/{PUBSUB_TOPIC}"
    s["pubsub_audience"] = push_endpoint
    state.save(_repo_root(), s)


# ---------------------------------------------------------------------------- #
# Step 9 — Cloudflare zone
# ---------------------------------------------------------------------------- #

def step_09_cf_zone(s: dict[str, Any]) -> None:
    prompts.step(9, TOTAL_STEPS, "Add Cloudflare zone")

    if "cf_api_token" not in s:
        prompts.info(
            "Mint an API token at https://dash.cloudflare.com/profile/api-tokens\n"
            "  Required scopes:\n"
            "    Account → Cloudflare Tunnel:Edit\n"
            "    Account → Zone:Create\n"
            "    Zone    → DNS:Edit (All zones)\n"
        )
        s["cf_api_token"] = prompts.ask("Cloudflare API token", secret=True)

    if "cf_account_id" not in s:
        accts = cloudflare.list_accounts(s["cf_api_token"])
        if len(accts) == 1:
            s["cf_account_id"] = accts[0]["id"]
            prompts.ok(f"Auto-selected Cloudflare account: {accts[0]['name']}")
        else:
            for a in accts:
                prompts.info(f"  {a['id']}  — {a.get('name')}")
            s["cf_account_id"] = prompts.ask("Enter Cloudflare account ID")

    zone = cloudflare.create_zone(s["cf_api_token"], s["domain"], s["cf_account_id"])
    s["cf_zone_id"] = zone["id"]
    s["cf_name_servers"] = zone.get("name_servers", [])
    state.save(_repo_root(), s)


# ---------------------------------------------------------------------------- #
# Step 10 — wait for zone activation (NS records propagated)
# ---------------------------------------------------------------------------- #

def step_10_wait_zone(s: dict[str, Any]) -> None:
    prompts.step(10, TOTAL_STEPS, "Wait for Cloudflare zone activation")

    status = cloudflare.zone_status(s["cf_api_token"], s["cf_zone_id"])
    if status == "active":
        prompts.ok("Zone already active")
        return

    prompts.info("Set these nameservers at your domain registrar:")
    for ns in s.get("cf_name_servers", []):
        prompts.info(f"  {ns}")
    prompts.info(
        "After Cloudflare confirms the change (email + dashboard banner), "
        "the zone status flips to 'active'. This can take minutes to hours."
    )

    if not prompts.wait_for(
        "Cloudflare zone activation",
        lambda: cloudflare.zone_status(s["cf_api_token"], s["cf_zone_id"]) == "active",
        interval_s=30.0,
        timeout_s=3600.0,
    ):
        prompts.die("Zone activation timed out. Resume with `setup_workspace` again later.")


# ---------------------------------------------------------------------------- #
# Step 11 — delete wildcard A records (Gotcha §7)
# ---------------------------------------------------------------------------- #

def step_11_delete_wildcards(s: dict[str, Any]) -> None:
    prompts.step(11, TOTAL_STEPS, "Delete any wildcard A records (Gotcha §7)")
    deleted = cloudflare.delete_wildcard_a_records(s["cf_api_token"], s["cf_zone_id"])
    if deleted == 0:
        prompts.ok("No wildcard A records present")


# ---------------------------------------------------------------------------- #
# Step 12 — Cloudflare tunnel: create + route + token
# ---------------------------------------------------------------------------- #

def step_12_tunnel(s: dict[str, Any]) -> None:
    prompts.step(12, TOTAL_STEPS, "Create Cloudflare tunnel + DNS route")

    uuid = cloudflare.create_tunnel(TUNNEL_NAME)
    cloudflare.route_dns(TUNNEL_NAME, s["hostname"])
    token = cloudflare.tunnel_token(TUNNEL_NAME)

    s["cf_tunnel_uuid"] = uuid
    s["cf_tunnel_token"] = token
    state.save(_repo_root(), s)

    # Public-hostname → service mapping must still be done in the dashboard.
    cloudflare.configure_tunnel_ingress(uuid, s["hostname"], "http://api:8000")
    prompts.press_enter("After adding the public hostname mapping in the dashboard")


# ---------------------------------------------------------------------------- #
# Step 13 — write .env and bring up the stack
# ---------------------------------------------------------------------------- #

def step_13_env_and_up(s: dict[str, Any]) -> None:
    prompts.step(13, TOTAL_STEPS, "Write .env and bring up docker compose")

    env_path = _repo_root() / ".env"

    env.set_values(
        env_path,
        {
            "WORKSPACE_DOMAIN": s["domain"],
            "INTERNAL_EMAILS_OR_DOMAINS": s["domain"],
            "CLOUDFLARE_TUNNEL_TOKEN": s["cf_tunnel_token"],
            "PUBSUB_TOPIC": s["pubsub_topic_full"],
            "PUBSUB_AUDIENCE": s["pubsub_audience"],
            "PUBSUB_SERVICE_ACCOUNT_EMAIL": s["sa_email"],
        },
    )
    prompts.ok(f"Wrote settings to {env_path}")

    _run_compose(["up", "-d", "--build"], timeout=600)
    prompts.ok("Docker stack is up")


# ---------------------------------------------------------------------------- #
# Step 14 — health check
# ---------------------------------------------------------------------------- #

def step_14_health(s: dict[str, Any]) -> None:
    prompts.step(14, TOTAL_STEPS, "Wait for /health endpoint")
    url = f"https://{s['hostname']}/health"

    def probe() -> bool:
        try:
            with urllib_request.urlopen(url, timeout=10) as resp:
                return resp.status == 200
        except urllib_error.URLError:
            return False

    if not prompts.wait_for(f"GET {url}", probe, interval_s=5.0, timeout_s=180.0):
        prompts.die(
            f"{url} never returned 200. Check:\n"
            "  - docker compose logs cloudflared\n"
            "  - dig @8.8.8.8 hub.<domain> +short (should return Cloudflare IPs)\n"
            "  - Tunnel's public-hostname mapping in the Cloudflare dashboard"
        )


# ---------------------------------------------------------------------------- #
# Step 15 — Notion (browser stop #2)
# ---------------------------------------------------------------------------- #

def step_15_notion(s: dict[str, Any]) -> None:
    prompts.step(15, TOTAL_STEPS, "Create Notion integration + share DBs (browser stop)")

    prompts.browser_stop(
        title="Notion integration",
        url="https://www.notion.so/profile/integrations",
        instructions=[
            "Click '+ New integration'.",
            f"Name: gb-automations    Workspace: the {s['domain']} workspace    Type: Internal",
            "Capabilities: Read content, Update content, Insert content, "
            "Read user information INCLUDING EMAIL ADDRESSES.",
            "Save → copy the 'Internal Integration Secret' (starts with `ntn_`).",
            "Then in Notion: open EACH of {Projects DB, Contacts DB, Emails parent page}, "
            "click ⋯ → Connections → add 'gb-automations'.",
        ],
        open_browser=True,
    )

    if "notion_token" not in s:
        token = prompts.ask("Paste the Notion integration secret", secret=True)
        s["notion_token"] = token
        state.save(_repo_root(), s)

    # Probe — the token must be valid AND see at least one database.
    def probe() -> bool:
        try:
            notion.whoami(s["notion_token"])
            dbs = notion.search_databases(s["notion_token"])
            return len(dbs) > 0
        except notion.NotionError:
            return False

    if not prompts.wait_for(
        "Notion token + at least one DB visible",
        probe,
        interval_s=10.0,
        timeout_s=600.0,
    ):
        prompts.die("Notion integration not visible. Did you share at least one DB with it?")


# ---------------------------------------------------------------------------- #
# Step 16 — discover Notion DB IDs and write to .env
# ---------------------------------------------------------------------------- #

def step_16_notion_dbs(s: dict[str, Any]) -> None:
    prompts.step(16, TOTAL_STEPS, "Discover Notion DB IDs")
    token = s["notion_token"]

    dbs = notion.search_databases(token)
    prompts.info(f"Integration can see {len(dbs)} database(s):")
    for db in dbs:
        title = "".join(p.get("plain_text", "") for p in db.get("title", []))
        prompts.info(f"  {db['id']}  — {title or '(untitled)'}")

    projects_id = notion.find_db_id_by_title(dbs, ["Project", "Projects"])
    contacts_id = notion.find_db_id_by_title(dbs, ["Contact", "Contacts"])

    if not projects_id:
        projects_id = prompts.ask("Could not auto-detect Projects DB; paste its ID")
    if not contacts_id:
        contacts_id = prompts.ask("Could not auto-detect Contacts DB; paste its ID")

    parent_page_id = notion.find_page_id_by_title(token, ["Emails", "Emails parent", "Mail"])
    if not parent_page_id:
        parent_page_id = prompts.ask(
            "Could not auto-detect the Emails parent PAGE; paste its ID "
            "(the page under which year-partitioned `Emails YYYY` DBs will live)"
        )

    env.set_values(
        _repo_root() / ".env",
        {
            "NOTION_TOKEN": token,
            "PROJECTS_DB_ID": projects_id,
            "CONTACTS_DB_ID": contacts_id,
            "EMAILS_PARENT_PAGE_ID": parent_page_id,
        },
    )

    _run_compose(["up", "-d", "--force-recreate", "api"], timeout=180)
    prompts.ok("Notion DB IDs wired into .env; api recreated")


# ---------------------------------------------------------------------------- #
# Step 17 — Notion webhook + verification token
# ---------------------------------------------------------------------------- #

def step_17_notion_webhook(s: dict[str, Any]) -> None:
    prompts.step(17, TOTAL_STEPS, "Register Notion webhook + capture verification token")

    if s.get("notion_webhook_done"):
        prompts.ok("Notion webhook already wired in prior run")
        return

    prompts.browser_stop(
        title="Notion webhook",
        url="https://www.notion.so/profile/integrations",
        instructions=[
            "Open the gb-automations integration → Webhooks → '+ Add subscription'.",
            f"Endpoint: https://{s['hostname']}/webhooks/notion",
            "Events: page.created AND page.properties_updated.",
            "Save.  Notion sends a verification POST — we'll capture the token from logs.",
        ],
    )

    prompts.info("Tailing api logs for the `Notion webhook verification` line…")
    token = _tail_for_verification_token(_repo_root(), timeout_s=300.0)
    if not token:
        prompts.die(
            "Didn't see a verification token in api logs within 5 min. Re-check:\n"
            "  - The endpoint URL is exactly https://{hostname}/webhooks/notion\n"
            "  - api logs: docker compose logs api | grep -i notion"
        )
    prompts.ok(f"Captured verification token ({token[:12]}…)")

    prompts.info("Paste this token into Notion's 'Verification token' field, then confirm:")
    print()
    print(f"    {token}")
    print()
    prompts.press_enter("After pasting + confirming in Notion")

    env.set_values(_repo_root() / ".env", {"NOTION_WEBHOOK_SECRET": token})
    _run_compose(["up", "-d", "--force-recreate", "api"], timeout=180)
    s["notion_webhook_done"] = True
    state.save(_repo_root(), s)


def _tail_for_verification_token(repo: Path, timeout_s: float) -> str | None:
    """Stream `docker compose logs -f api` and pluck the `secret_…` token.

    Notion's verification POST is logged once per registration. We start the
    tail BEFORE the operator clicks Save in step 17 (well, technically we
    start after — there's a small race, but `docker compose logs` reads from
    container start by default with no --since flag, so we'd see it even if
    it landed during the previous step).
    """
    cmd = ["docker", "compose", "logs", "--no-color", "--since=10m", "api"]
    proc = subprocess.Popen(
        cmd,
        cwd=repo,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    deadline = time.monotonic() + timeout_s
    needle = "Notion webhook verification token"
    found: str | None = None
    try:
        assert proc.stdout is not None
        # First pass: drain the existing buffer.
        for line in proc.stdout:
            if needle in line:
                found = _extract_secret(line)
                if found:
                    return found
            if time.monotonic() > deadline:
                break
        # If we drained without finding it, switch to follow mode.
        proc.terminate()
        proc.wait(timeout=5)
        follow = subprocess.Popen(
            ["docker", "compose", "logs", "-f", "--no-color", "--since=10m", "api"],
            cwd=repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        try:
            assert follow.stdout is not None
            while time.monotonic() < deadline:
                line = follow.stdout.readline()
                if not line:
                    time.sleep(0.5)
                    continue
                if needle in line:
                    found = _extract_secret(line)
                    if found:
                        return found
            return None
        finally:
            follow.terminate()
            try:
                follow.wait(timeout=5)
            except subprocess.TimeoutExpired:
                follow.kill()
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
    return found


def _extract_secret(line: str) -> str | None:
    """Find a `secret_…` token in a log line."""
    idx = line.find("secret_")
    if idx < 0:
        return None
    tail = line[idx:].strip()
    # Token is alphanumeric + underscore, no spaces. Stop at first whitespace.
    end = 0
    while end < len(tail) and (tail[end].isalnum() or tail[end] == "_"):
        end += 1
    token = tail[:end]
    return token if len(token) > len("secret_") + 8 else None


# ---------------------------------------------------------------------------- #
# Step 18 — seed users, start watches, backfill labels, pull LLM model
# ---------------------------------------------------------------------------- #

def step_18_seed(s: dict[str, Any]) -> None:
    prompts.step(18, TOTAL_STEPS, "Seed users + Gmail watches + label backfill + LLM model")

    raw = prompts.ask(
        f"Workspace user emails to sync (comma-separated, all on @{s['domain']})"
    )
    emails = [e.strip() for e in raw.split(",") if e.strip()]
    if not emails:
        prompts.die("At least one user email required.")

    _exec_in_api(["python", "-m", "gb_automations.scripts.seed_users", *emails])
    _exec_in_api(["python", "-m", "gb_automations.scripts.start_watches"])
    _exec_in_api(["python", "-m", "gb_automations.scripts.backfill_project_labels"])

    if prompts.confirm("Pull the ~5GB Ollama LLM model now? (slow, but only once)", default=True):
        _exec_in_api(["python", "-m", "gb_automations.scripts.pull_llm_model"])
    else:
        prompts.warn(
            "Skipped. Run later: "
            "docker compose exec api python -m gb_automations.scripts.pull_llm_model"
        )


# ---------------------------------------------------------------------------- #
# Step 19 — smoke test
# ---------------------------------------------------------------------------- #

def step_19_smoke(s: dict[str, Any]) -> None:
    prompts.step(19, TOTAL_STEPS, "Smoke test")
    hostname = s["hostname"]

    for path in ("/health", "/debug/databases"):
        try:
            with urllib_request.urlopen(f"https://{hostname}{path}", timeout=15) as resp:
                if resp.status == 200:
                    prompts.ok(f"GET {path} → 200")
                else:
                    prompts.warn(f"GET {path} → {resp.status}")
        except urllib_error.URLError as e:
            prompts.warn(f"GET {path} failed: {e}")

    prompts.heading("Setup complete")
    prompts.info(f"  Domain:    {s['domain']}")
    prompts.info(f"  Hub URL:   https://{hostname}")
    prompts.info(f"  GCP proj:  {s['project_id']}")
    prompts.info(f"  Tunnel:    {TUNNEL_NAME}")
    print()
    prompts.info("Next steps:")
    prompts.info("  - Add a row to the Notion Projects DB → confirm a Gmail label appears.")
    prompts.info("  - Label an email with a project label → confirm it lands in Notion.")
    prompts.info(
        "  - Frame.io integration will be added by the installer in a follow-up "
        "next week."
    )


# ---------------------------------------------------------------------------- #
# Helpers — docker compose / exec
# ---------------------------------------------------------------------------- #

def _run_compose(args: list[str], *, timeout: int = 180) -> None:
    repo = _repo_root()
    cmd = ["docker", "compose", *args]
    prompts.info(f"$ {' '.join(cmd)}")
    try:
        subprocess.run(cmd, cwd=repo, check=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        prompts.die(f"docker compose failed: {e}")


def _exec_in_api(cmd: list[str]) -> None:
    repo = _repo_root()
    full = ["docker", "compose", "exec", "-T", "api", *cmd]
    prompts.info(f"$ {' '.join(full)}")
    try:
        subprocess.run(full, cwd=repo, check=True)
    except subprocess.CalledProcessError as e:
        prompts.die(f"Command failed inside api container: {e}")


# ---------------------------------------------------------------------------- #
# Main
# ---------------------------------------------------------------------------- #

# Step 0 (preflight) and step 1 (collect inputs) have unique signatures, so
# they're called directly in main(). Step 2 onwards all take `state` and
# return None.
STATEFUL_STEPS = [
    step_02_orgpolicy_admin,
    step_03_project,
    step_04_apis,
    step_05_org_policies,
    step_06_service_account,
    step_07_dwd,
    step_08_pubsub,
    step_09_cf_zone,
    step_10_wait_zone,
    step_11_delete_wildcards,
    step_12_tunnel,
    step_13_env_and_up,
    step_14_health,
    step_15_notion,
    step_16_notion_dbs,
    step_17_notion_webhook,
    step_18_seed,
    step_19_smoke,
]


def main() -> None:
    repo = _repo_root()
    prompts.heading(f"gb-automations installer  (repo: {repo})")
    s = state.load(repo)
    if s:
        prompts.info(f"Resuming from .setup_state.json (domain={s.get('domain', '?')})")

    try:
        step_00_preflight()
        s = step_01_collect_inputs(s)
        for fn in STATEFUL_STEPS:
            fn(s)
    except KeyboardInterrupt:
        print()
        prompts.warn("Interrupted. Re-run the installer to resume.")
        sys.exit(130)


if __name__ == "__main__":
    main()
