"""Cloudflare integration: REST for zones + DNS, `cloudflared` CLI for tunnel.

Zone creation and DNS-record edits use the REST API (the `cloudflared` binary
doesn't cover them). Tunnel create + DNS route + token retrieval use the
binary (the REST equivalents need an Account API token with broader scopes
and produce a less convenient token format).

The single API token the installer asks for needs:
  - Account → Zone → Create
  - Zone → DNS → Edit
on the relevant account. (No API exists for minting API tokens themselves.)
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from . import prompts

_API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareError(RuntimeError):
    pass


def check_cloudflared_installed() -> None:
    if not shutil.which("cloudflared"):
        raise CloudflareError(
            "cloudflared not found on PATH. Install: brew install cloudflared"
        )


def check_cloudflared_authed() -> None:
    """cloudflared writes ~/.cloudflared/cert.pem after `cloudflared tunnel login`."""
    from pathlib import Path

    cert = Path.home() / ".cloudflared" / "cert.pem"
    if not cert.exists():
        raise CloudflareError(
            "cloudflared not authenticated. Run: cloudflared tunnel login\n"
            "  (Opens browser; you select the zone you'll manage.)"
        )


# --------------------------------- REST ----------------------------------- #


def _api(
    api_token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    url = f"{_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib_request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except urllib_error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        raise CloudflareError(f"Cloudflare API {method} {path} → HTTP {e.code}: {raw}") from e
    except urllib_error.URLError as e:
        raise CloudflareError(f"Cloudflare API connection failed: {e}") from e

    parsed = json.loads(raw)
    if not parsed.get("success"):
        errors = parsed.get("errors", [])
        raise CloudflareError(f"Cloudflare API error: {errors}")
    return parsed


def list_accounts(api_token: str) -> list[dict[str, Any]]:
    """Return [{'id': '...', 'name': '...'}, ...] for accounts the token can see."""
    resp = _api(api_token, "GET", "/accounts")
    return resp.get("result", [])


def find_zone(api_token: str, domain: str) -> dict[str, Any] | None:
    resp = _api(api_token, "GET", f"/zones?name={domain}")
    results = resp.get("result", [])
    return results[0] if results else None


def create_zone(api_token: str, domain: str, account_id: str) -> dict[str, Any]:
    """Create a zone. Returns the zone object (including `name_servers`)."""
    existing = find_zone(api_token, domain)
    if existing:
        prompts.ok(f"Cloudflare zone {domain} already exists ({existing['id']})")
        return existing
    resp = _api(
        api_token,
        "POST",
        "/zones",
        body={"name": domain, "account": {"id": account_id}, "type": "full"},
    )
    zone = resp["result"]
    prompts.ok(f"Created Cloudflare zone {domain} ({zone['id']})")
    return zone


def zone_status(api_token: str, zone_id: str) -> str:
    resp = _api(api_token, "GET", f"/zones/{zone_id}")
    return resp["result"]["status"]  # "pending" | "active" | "moved" | ...


def list_dns_records(api_token: str, zone_id: str) -> list[dict[str, Any]]:
    resp = _api(api_token, "GET", f"/zones/{zone_id}/dns_records?per_page=100")
    return resp.get("result", [])


def delete_wildcard_a_records(api_token: str, zone_id: str) -> int:
    """Delete every `A *` record. Returns the number deleted.

    Wildcard A records intercept new subdomains and route them to the wrong
    origin — see docs/gotchas.md §7. Always delete on a fresh install.
    """
    records = list_dns_records(api_token, zone_id)
    targets = [r for r in records if r["type"] == "A" and r["name"].startswith("*.")]
    for r in targets:
        _api(api_token, "DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")
        prompts.ok(f"Deleted wildcard {r['type']} record {r['name']} → {r['content']}")
    return len(targets)


# ----------------------------- cloudflared CLI ---------------------------- #


def tunnel_exists(tunnel_name: str) -> str | None:
    """Return the tunnel UUID if a tunnel with this name exists, else None."""
    try:
        out = subprocess.run(
            ["cloudflared", "tunnel", "list", "--output", "json"],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        ).stdout
    except subprocess.CalledProcessError as e:
        raise CloudflareError(f"cloudflared tunnel list failed: {e.stderr}") from e

    for tunnel in json.loads(out or "[]"):
        if tunnel.get("name") == tunnel_name and not tunnel.get("deleted_at"):
            return tunnel["id"]
    return None


def create_tunnel(tunnel_name: str) -> str:
    """Create a named tunnel. Returns its UUID. Idempotent."""
    existing = tunnel_exists(tunnel_name)
    if existing:
        prompts.ok(f"Cloudflare tunnel {tunnel_name} already exists ({existing})")
        return existing
    try:
        out = subprocess.run(
            ["cloudflared", "tunnel", "create", tunnel_name],
            check=True,
            text=True,
            capture_output=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as e:
        raise CloudflareError(f"cloudflared tunnel create failed: {e.stderr}") from e

    # Re-read to capture UUID (cloudflared's output formatting varies by version).
    uuid = tunnel_exists(tunnel_name)
    if not uuid:
        raise CloudflareError(f"Tunnel create succeeded but couldn't find UUID:\n{out.stdout}")
    prompts.ok(f"Created tunnel {tunnel_name} ({uuid})")
    return uuid


def route_dns(tunnel_name: str, hostname: str) -> None:
    """Create the DNS CNAME pointing `hostname` at the tunnel.

    `cloudflared tunnel route dns` is idempotent ONLY when the existing CNAME
    already points at this tunnel. If a different record exists, it errors —
    we surface that as a Gotcha so the operator can investigate before we
    clobber whatever is there.
    """
    try:
        subprocess.run(
            ["cloudflared", "tunnel", "route", "dns", tunnel_name, hostname],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or "").strip()
        if "already exists" in stderr.lower() or "already points" in stderr.lower():
            prompts.ok(f"DNS route {hostname} → tunnel already in place")
            return
        raise CloudflareError(
            f"cloudflared tunnel route dns failed: {stderr}\n"
            f"  → Check Cloudflare DNS for an existing record on {hostname}."
        ) from e
    prompts.ok(f"Routed {hostname} → tunnel {tunnel_name}")


def tunnel_token(tunnel_name: str) -> str:
    """Return the eyJ... JWT that docker-compose's cloudflared service needs."""
    try:
        out = subprocess.run(
            ["cloudflared", "tunnel", "token", tunnel_name],
            check=True,
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.CalledProcessError as e:
        raise CloudflareError(f"cloudflared tunnel token failed: {e.stderr}") from e
    token = out.stdout.strip()
    if not token.startswith("eyJ"):
        raise CloudflareError(f"Unexpected token format: {token[:40]}…")
    return token


def configure_tunnel_ingress(tunnel_uuid: str, hostname: str, service: str) -> None:
    """Set the tunnel's public-hostname → service mapping via the management API.

    `cloudflared tunnel route dns` only sets the DNS CNAME; the tunnel itself
    also needs an *ingress* rule saying "traffic for hub.example.com goes to
    api:8000 inside the docker network." For tunnels created with the
    cloudflared CLI the canonical place to set this is the dashboard UI;
    however the same can be done via the Cloudflare-for-Teams API, which
    needs an Account API token. We do it via cloudflared's `tunnel ingress
    validate` config when we lay down the YAML in a future iteration.

    For now we emit a clear instruction so the operator finishes the wire-up
    in the dashboard (a UI step that takes ~10 seconds). This is the one
    small click we can't automate without a broader API token.
    """
    prompts.warn(
        "Public-hostname → service mapping must be set in the Cloudflare "
        "dashboard once:\n"
        f"    Zero Trust → Networks → Tunnels → {tunnel_uuid[:8]}… → Public Hostnames → Add\n"
        f"      Subdomain: {hostname.split('.', 1)[0]}\n"
        f"      Domain:    {hostname.split('.', 1)[1] if '.' in hostname else hostname}\n"
        "      Type:      HTTP\n"
        f"      URL:       {service}"
    )
