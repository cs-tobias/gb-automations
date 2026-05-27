"""One-time Toggl Track bootstrap.

Goldbox uses a single workspace-admin Toggl API token for the integration —
Toggl's Reports v3 API returns every workspace member's entries when called
by an admin, so there's no need for per-user tokens. This script does the
one-time chores you can't skip:

  1. Validates that TOGGL_API_TOKEN works (GET /api/v9/me).
  2. Lists workspaces the token can see and lets you pick one (auto-picks
     when there's only one — typical for Goldbox).
  3. Prints the TOGGL_WORKSPACE_ID line to paste into .env.
  4. Lists workspace members with their toggl_user_id, email, and name —
     copy this somewhere; you'll need it when wiring the user map between
     `Kontaktpersoner` (Notion) and Toggl users in a later step.
  5. Shows a preview of existing Toggl projects so you can spot drift before
     flipping SYNC_TOGGL=true.

Run inside the api container:

    docker compose exec api python -m gb_automations.scripts.toggl_bootstrap

Re-run any time the token is rotated — the script is idempotent and
side-effect-free (read-only Toggl calls; never modifies .env directly).

Why not write .env automatically? Same reason as the Frame bootstrap: the
file lives on the host, not in the container, and copy-paste is more
transparent than a host-mount round-trip.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from gb_automations.clients import toggl
from gb_automations.config import settings


async def main() -> None:
    if not settings.toggl_api_token:
        _die(
            "TOGGL_API_TOKEN must be set in .env before running the bootstrap.\n"
            "  Get it from: https://track.toggl.com/profile\n"
            "  (Sign in as the Goldbox Toggl admin first.)"
        )

    print()
    print("=" * 76)
    print("Toggl Track bootstrap")
    print("=" * 76)
    print()
    print("Step 1 — Validating token (GET /api/v9/me)…")

    try:
        me = await toggl.whoami()
    except httpx.HTTPError as err:
        _die(f"Whoami call failed: {err}")
    except toggl.TogglAPIError as err:
        if err.status_code in (401, 403):
            _die(
                f"Toggl rejected the token ({err.status_code}). Double-check "
                f"TOGGL_API_TOKEN in .env — it should be the 32-char hex "
                f"string from https://track.toggl.com/profile."
            )
        _die(str(err))

    email = me.get("email") or "(unknown)"
    fullname = me.get("fullname") or me.get("name") or "(unknown)"
    print(f"  Authenticated as: {fullname} <{email}>")

    workspaces = me.get("workspaces") or []
    if not workspaces:
        _die(
            "This user has no Toggl workspaces. Check the sign-in — the "
            "admin needs to be a member of the Goldbox workspace."
        )

    print()
    print("Step 2 — Picking workspace…")
    workspace = _pick("workspace", workspaces)
    workspace_id = str(workspace["id"])

    # Stash on settings so subsequent toggl.* calls in this script have it.
    settings.toggl_workspace_id = workspace_id

    print()
    print("Step 3 — Listing workspace members…")
    try:
        users = await toggl.list_workspace_users(workspace_id)
    except toggl.TogglAPIError as err:
        _die(f"Could not list workspace users: {err}")

    if not users:
        print("  (No members found — unusual; check workspace permissions.)")
    else:
        print(f"  Found {len(users)} member(s):")
        print()
        print(f"  {'toggl_user_id':<14} {'email':<35} name")
        print(f"  {'-' * 14} {'-' * 35} {'-' * 30}")
        for u in users:
            uid = str(u.get("id", ""))
            uemail = (u.get("email") or "")[:34]
            uname = u.get("name") or u.get("fullname") or ""
            admin = " (admin)" if u.get("admin") else ""
            print(f"  {uid:<14} {uemail:<35} {uname}{admin}")

    print()
    print("Step 4 — Previewing existing Toggl projects…")
    try:
        projects = await toggl.list_projects(workspace_id)
        print(f"  Workspace currently contains {len(projects)} project(s).")
        if projects:
            preview = ", ".join(p.get("name", "(no name)") for p in projects[:5])
            print(f"  First few: {preview}{'…' if len(projects) > 5 else ''}")
    except toggl.TogglAPIError as err:
        print(f"  (Could not list projects for preview: {err}; non-fatal.)")

    print()
    print("=" * 76)
    print("DONE — paste this into .env (host machine), then restart the api:")
    print("=" * 76)
    print()
    print(f"TOGGL_WORKSPACE_ID={workspace_id}")
    print()
    print("Then on the host:")
    print("  docker compose up -d --force-recreate api")
    print()
    print("Next: keep the toggl_user_id list above. The next slice (Notion ↔")
    print("Toggl user map) will use those ids to attribute each day's hours to")
    print("the right `Kontaktpersoner` row.")
    print()


def _pick(label: str, items: list[dict]) -> dict:
    """Interactive picker. Auto-selects when there's only one option."""
    if len(items) == 1:
        only = items[0]
        print(
            f"  Single {label} found: {only.get('name')!r} ({only['id']}) — using it."
        )
        return only

    print()
    print(f"  Multiple {label}s — pick one:")
    for i, item in enumerate(items, start=1):
        print(f"    {i}. {item.get('name', '(no name)')} — {item['id']}")
    while True:
        raw = input(f"  Enter number (1-{len(items)}): ").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(items):
                return items[idx]
        except ValueError:
            pass
        print("  Invalid choice, try again.")


def _die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
