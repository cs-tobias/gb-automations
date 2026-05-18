"""`gcloud` wrappers: projects, services, org policies, service accounts, Pub/Sub.

Every function here is a thin subprocess wrapper. We use `--format=json` and
parse stdout so we don't depend on Google's Python SDK on the host machine.

Idempotency: every "create" function first checks if the resource already
exists (via `describe`) and returns success without re-creating. This lets
the installer be re-run safely after partial failures.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from . import prompts


class GcpError(RuntimeError):
    pass


def check_installed() -> None:
    if not shutil.which("gcloud"):
        raise GcpError(
            "gcloud not found on PATH. Install: brew install --cask google-cloud-sdk"
        )


def check_authed() -> str:
    """Return the active account email, or raise if not logged in."""
    out = _run_json(["gcloud", "auth", "list", "--format=json"])
    active = next((a for a in out if a.get("status") == "ACTIVE"), None)
    if not active:
        raise GcpError("No active gcloud account. Run: gcloud auth login")
    return active["account"]


def list_organizations() -> list[dict[str, Any]]:
    """Returns [{'displayName': 'goldbox.no', 'name': 'organizations/12345', ...}, ...]."""
    return _run_json(["gcloud", "organizations", "list", "--format=json"])


def list_billing_accounts() -> list[dict[str, Any]]:
    return _run_json(["gcloud", "billing", "accounts", "list", "--format=json"])


def project_exists(project_id: str) -> bool:
    try:
        _run_json(["gcloud", "projects", "describe", project_id, "--format=json"])
        return True
    except GcpError:
        return False


def create_project(project_id: str, name: str, org_id: str) -> None:
    """Create the project under the given org. No-op if it already exists."""
    if project_exists(project_id):
        prompts.ok(f"GCP project {project_id} already exists")
        return
    _run(
        [
            "gcloud",
            "projects",
            "create",
            project_id,
            "--name",
            name,
            "--organization",
            org_id,
        ]
    )
    prompts.ok(f"Created GCP project {project_id}")


def link_billing(project_id: str, billing_account_id: str) -> None:
    """Idempotent — linking the same account again is a no-op."""
    _run(
        [
            "gcloud",
            "billing",
            "projects",
            "link",
            project_id,
            "--billing-account",
            billing_account_id,
        ]
    )
    prompts.ok(f"Linked billing account {billing_account_id} to {project_id}")


def enable_services(project_id: str, services: list[str]) -> None:
    """Enable a batch of APIs. `gcloud services enable` is idempotent."""
    _run(["gcloud", "services", "enable", *services, "--project", project_id])
    prompts.ok(f"Enabled {len(services)} API(s)")


def grant_org_role(org_id: str, member: str, role: str) -> None:
    """`add-iam-policy-binding` is idempotent — re-running is a no-op."""
    _run(
        [
            "gcloud",
            "organizations",
            "add-iam-policy-binding",
            org_id,
            "--member",
            member,
            "--role",
            role,
            "--condition",
            "None",
        ],
        # This command prints the full updated policy to stderr — suppress.
        capture_stderr=True,
    )
    prompts.ok(f"Granted {role} to {member} on org {org_id}")


def disable_org_policy(project_id: str, constraint: str) -> None:
    """Override an org-wide policy to disabled at the project scope.

    `disable-enforce` works for boolean constraints (e.g.
    `iam.disableServiceAccountKeyCreation`). For list constraints (e.g.
    `iam.allowedPolicyMemberDomains`) the caller must use `set_org_policy_allow_all`.
    """
    _run(
        [
            "gcloud",
            "resource-manager",
            "org-policies",
            "disable-enforce",
            constraint,
            "--project",
            project_id,
        ]
    )
    prompts.ok(f"Disabled enforcement of {constraint} on {project_id}")


def set_org_policy_allow_all(project_id: str, constraint: str, tmp_dir: Path) -> None:
    """Set a list-constraint org policy to allowAll at the project scope.

    Specifically used for `iam.allowedPolicyMemberDomains`, which otherwise
    rejects adding `gmail-api-push@system.gserviceaccount.com` as a topic
    publisher.
    """
    yaml_body = (
        f"name: projects/{project_id}/policies/{constraint}\n"
        "spec:\n"
        "  rules:\n"
        "  - allowAll: true\n"
    )
    yaml_path = tmp_dir / f"{constraint.replace('.', '-')}-allow-all.yaml"
    yaml_path.write_text(yaml_body, encoding="utf-8")
    _run(["gcloud", "org-policies", "set-policy", str(yaml_path)])
    prompts.ok(f"Set {constraint} = allowAll on {project_id}")


def service_account_email(project_id: str, sa_id: str) -> str:
    return f"{sa_id}@{project_id}.iam.gserviceaccount.com"


def service_account_exists(project_id: str, sa_id: str) -> bool:
    email = service_account_email(project_id, sa_id)
    try:
        _run_json(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "describe",
                email,
                "--project",
                project_id,
                "--format=json",
            ]
        )
        return True
    except GcpError:
        return False


def create_service_account(project_id: str, sa_id: str, display_name: str) -> str:
    """Returns the SA's unique 21-digit ID (required for DWD authorization)."""
    if not service_account_exists(project_id, sa_id):
        _run(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "create",
                sa_id,
                "--display-name",
                display_name,
                "--project",
                project_id,
            ]
        )
        prompts.ok(f"Created service account {sa_id}")
    else:
        prompts.ok(f"Service account {sa_id} already exists")

    info = _run_json(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "describe",
            service_account_email(project_id, sa_id),
            "--project",
            project_id,
            "--format=json",
        ]
    )
    return info["uniqueId"]


def create_sa_key(project_id: str, sa_id: str, output_path: Path) -> None:
    """Create + download a JSON key. Skips creation if the file already exists."""
    if output_path.exists() and output_path.stat().st_size > 0:
        prompts.ok(f"Service account key already at {output_path}")
        return
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "keys",
            "create",
            str(output_path),
            "--iam-account",
            service_account_email(project_id, sa_id),
            "--project",
            project_id,
        ]
    )
    output_path.chmod(0o600)
    prompts.ok(f"Wrote key to {output_path}")


def grant_token_creator(project_id: str, sa_email: str) -> None:
    """Pub/Sub push with OIDC auth needs `roles/iam.serviceAccountTokenCreator`
    on the SA used as the push identity. Granting the SA on itself is the
    pattern GCP's docs use."""
    _run(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "add-iam-policy-binding",
            sa_email,
            "--member",
            f"serviceAccount:{sa_email}",
            "--role",
            "roles/iam.serviceAccountTokenCreator",
            "--project",
            project_id,
        ],
        capture_stderr=True,
    )
    prompts.ok(f"Granted tokenCreator to {sa_email}")


def pubsub_topic_exists(project_id: str, topic: str) -> bool:
    try:
        _run_json(
            [
                "gcloud",
                "pubsub",
                "topics",
                "describe",
                topic,
                "--project",
                project_id,
                "--format=json",
            ]
        )
        return True
    except GcpError:
        return False


def create_pubsub_topic(project_id: str, topic: str) -> None:
    if pubsub_topic_exists(project_id, topic):
        prompts.ok(f"Pub/Sub topic {topic} already exists")
        return
    _run(["gcloud", "pubsub", "topics", "create", topic, "--project", project_id])
    prompts.ok(f"Created Pub/Sub topic {topic}")


def grant_topic_publisher(project_id: str, topic: str, member: str) -> None:
    _run(
        [
            "gcloud",
            "pubsub",
            "topics",
            "add-iam-policy-binding",
            topic,
            "--member",
            member,
            "--role",
            "roles/pubsub.publisher",
            "--project",
            project_id,
        ],
        capture_stderr=True,
    )
    prompts.ok(f"Granted publisher to {member} on {topic}")


def pubsub_subscription_exists(project_id: str, sub: str) -> bool:
    try:
        _run_json(
            [
                "gcloud",
                "pubsub",
                "subscriptions",
                "describe",
                sub,
                "--project",
                project_id,
                "--format=json",
            ]
        )
        return True
    except GcpError:
        return False


def create_push_subscription(
    project_id: str,
    sub: str,
    topic: str,
    push_endpoint: str,
    push_auth_sa_email: str,
    audience: str,
) -> None:
    if pubsub_subscription_exists(project_id, sub):
        prompts.ok(f"Pub/Sub subscription {sub} already exists")
        return
    _run(
        [
            "gcloud",
            "pubsub",
            "subscriptions",
            "create",
            sub,
            "--topic",
            topic,
            "--push-endpoint",
            push_endpoint,
            "--push-auth-service-account",
            push_auth_sa_email,
            "--push-auth-token-audience",
            audience,
            "--project",
            project_id,
        ]
    )
    prompts.ok(f"Created push subscription {sub}")


# ----------------------------- private helpers ---------------------------- #


def _run(cmd: list[str], *, capture_stderr: bool = False) -> str:
    """Run a gcloud command; return stdout. Raise GcpError on non-zero exit."""
    try:
        result = subprocess.run(
            cmd,
            check=True,
            text=True,
            capture_output=True,
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        msg = e.stderr.strip() if e.stderr else str(e)
        if capture_stderr:
            # Some gcloud commands print success info to stderr — caller asked
            # us to swallow it, but if exit code is non-zero it's still an error.
            pass
        raise GcpError(f"Command failed: {' '.join(cmd[:3])}…\n  {msg}") from e
    except FileNotFoundError as e:
        raise GcpError(f"gcloud not found: {e}") from e


def _run_json(cmd: list[str]) -> Any:
    out = _run(cmd).strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise GcpError(f"Could not parse gcloud JSON output: {e}\n{out[:500]}") from e
