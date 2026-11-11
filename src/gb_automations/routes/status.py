"""`GET /status` — the operator's "is it working?" page.

The desktop icon that `deploy/windows/Install.ps1` creates (Start-GbAutomations)
starts the stack if needed and opens this page, so it is the one screen a
non-technical person at the workstation looks at. One row per integration,
green / amber / red, each red row carrying a one-line fix hint that points at
docs/move-to-new-pc.md. Checks reuse the same client calls as the /debug smoke
tests but each is bounded by a timeout and isolated, so one hung integration
can't blank the whole page.

Local-only on purpose: the Cloudflare tunnel forwards every path on
hub.<domain> to this app, and every request arriving through it carries a
`Cf-Connecting-Ip` header that a request made on the PC itself never has. The
page names mailboxes and integrations, which the public internet doesn't need
to see.
"""

from __future__ import annotations

import asyncio
import html
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from gb_automations.config import settings

router = APIRouter(tags=["status"])

CHECK_TIMEOUT_S = 10.0
GUIDE = "docs/move-to-new-pc.md"

Level = Literal["ok", "warn", "fail"]


class CheckWarning(Exception):
    """Raised by a check that works but needs a human's attention (amber row)."""


@dataclass(frozen=True)
class Check:
    name: str
    run: Callable[[], Awaitable[str]]
    hint: str


@dataclass(frozen=True)
class CheckResult:
    name: str
    level: Level
    detail: str
    hint: str


async def _check_database() -> str:
    from sqlalchemy import text

    from gb_automations.db import engine

    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return "connected"


async def _active_mailboxes() -> list[str]:
    from sqlalchemy import select

    from gb_automations.db import SessionLocal
    from gb_automations.models import User

    async with SessionLocal() as session:
        rows = await session.execute(select(User.email).where(User.active.is_(True)))
    return sorted(rows.scalars().all())


async def _check_gmail() -> str:
    from gb_automations.clients import gmail as gmail_client

    mailboxes = await _active_mailboxes()
    if not mailboxes:
        raise RuntimeError("no active mailboxes (SYNCED_MAILBOXES in .env is empty?)")

    # Proves the service-account key file is in place AND domain-wide delegation
    # still works — the two things a PC move can break for Gmail.
    def _profile() -> dict:
        return gmail_client.gmail_for(mailboxes[0]).users().getProfile(userId="me").execute()

    await asyncio.to_thread(_profile)
    return f"{len(mailboxes)} mailbox(es): {', '.join(mailboxes)}"


async def _check_notion() -> str:
    from gb_automations.clients import notion as notion_client

    me = await notion_client.whoami()
    return f"signed in as {me.get('name') or 'integration'}"


async def _check_ollama() -> str:
    base = settings.ollama_base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_S) as client:
        resp = await client.get(f"{base}/api/tags")
        resp.raise_for_status()
    names = {m.get("name") for m in resp.json().get("models") or []}
    if settings.ollama_model not in names:
        raise CheckWarning(
            f"Ollama runs, but model {settings.ollama_model} isn't downloaded yet "
            "(the app pulls it on start; ~5 GB)"
        )
    return f"model {settings.ollama_model} ready"


async def _check_nas() -> str:
    from gb_automations.clients import nas as nas_client

    if not await asyncio.to_thread(nas_client.nas_available):
        raise RuntimeError(f"{settings.nas_projects_root or '(NAS_PROJECTS_ROOT unset)'} not writable")
    return f"{settings.nas_projects_root} mounted and writable"


async def _check_frame() -> str:
    from gb_automations.clients import frame as frame_client

    me = await frame_client.whoami()
    profile = me.get("data") if isinstance(me, dict) and "data" in me else me
    return f"signed in as {profile.get('email') or profile.get('name') or 'Frame user'}"


async def _check_fiken() -> str:
    from gb_automations.clients import fiken as fiken_client

    user = await fiken_client.whoami()
    return f"signed in as {user.get('name') or user.get('email') or 'Fiken user'}"


async def _check_toggl() -> str:
    from gb_automations.clients import toggl as toggl_client

    me = await toggl_client.whoami()
    return f"signed in as {me.get('fullname') or me.get('email') or 'Toggl user'}"


async def _check_queue() -> str:
    from gb_automations.sync.queue import queue_counts

    q = await queue_counts(failed_limit=1)
    counts = q["counts"]
    summary = f"{counts['pending']} waiting, {counts['in_progress']} running, {counts['failed']} failed"
    if counts["failed"]:
        raise CheckWarning(summary)
    age = q.get("oldest_pending_age_seconds")
    if age is not None and age > 3600:
        raise CheckWarning(f"{summary}; oldest waiting task is {int(age // 60)} min old")
    return summary


async def _check_notion_automations() -> str:
    from gb_automations.routes.webhooks import _NOTION_AUTOMATION_LAST_SEEN

    seen = {name: dict(entry) for name, entry in _NOTION_AUTOMATION_LAST_SEEN.items()}
    if not seen:
        # In-memory and reset on every restart, so empty is normal right after
        # a start — amber, not red.
        raise CheckWarning("none has fired since the app started")
    if any(entry.get("last_action") == "auth_failed" for entry in seen.values()):
        raise CheckWarning("receiving calls, but the secret doesn't match (auth_failed)")
    return ", ".join(
        f"{name} {(entry.get('last_seen_utc') or '')[:16]}Z" for name, entry in sorted(seen.items())
    )


def _checks() -> list[Check]:
    checks = [
        Check("Database", _check_database, "The db container isn't up. Double-click the GB Automations icon, "
              f"or see {GUIDE} (Troubleshooting)."),
        Check("Gmail", _check_gmail, "Check secrets\\gcp-service-account.json is present and SYNCED_MAILBOXES "
              f"is set in .env. See {GUIDE}."),
        Check("Notion", _check_notion, "NOTION_TOKEN in .env is missing or revoked."),
        Check("AI tagging (Ollama)", _check_ollama, "Start the Ollama app (Start menu). Emails still sync "
              "without it, just untagged."),
        Check("Work queue", _check_queue, "Failed tasks: fix the cause, then POST /debug/queue/retry-failed."),
        Check("Notion automations", _check_notion_automations, "If this stays empty after someone clicks a "
              "Notion button, open the Automations panel in Notion and re-enable any paused automation "
              "(docs/misc/notion-setup.md Part 6e)."),
    ]
    if settings.sync_nas_folders:
        checks.append(Check("NAS (W: drive)", _check_nas, "This PC must be on the office network. See "
                            "docs/misc/gotchas.md section 15."))
    if settings.sync_frame:
        checks.append(Check("Frame.io", _check_frame, "Frame sign-in expired: run "
                            "`docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap` "
                            f"(see {GUIDE})."))
    if settings.sync_fiken:
        checks.append(Check("Fiken", _check_fiken, "FIKEN_API_TOKEN in .env is missing or revoked."))
    if settings.sync_toggl or settings.sync_toggl_hours:
        checks.append(Check("Toggl", _check_toggl, "TOGGL_API_TOKEN in .env is missing or revoked."))
    return checks


async def _run_one(check: Check) -> CheckResult:
    try:
        detail = await asyncio.wait_for(check.run(), timeout=CHECK_TIMEOUT_S)
        return CheckResult(check.name, "ok", detail, "")
    except CheckWarning as warn:
        return CheckResult(check.name, "warn", str(warn), check.hint)
    except TimeoutError:
        return CheckResult(check.name, "fail", f"no answer within {CHECK_TIMEOUT_S:.0f}s", check.hint)
    except Exception as err:
        message = str(err).strip() or type(err).__name__
        return CheckResult(check.name, "fail", message[:300], check.hint)


async def run_checks() -> list[CheckResult]:
    return list(await asyncio.gather(*(_run_one(c) for c in _checks())))


_COLORS = {"ok": "#1a7f37", "warn": "#9a6700", "fail": "#cf222e"}
_LABELS = {"ok": "OK", "warn": "Check", "fail": "Problem"}


def render_html(results: list[CheckResult]) -> str:
    worst = "fail" if any(r.level == "fail" for r in results) else (
        "warn" if any(r.level == "warn" for r in results) else "ok"
    )
    headline = {
        "ok": "Everything is running",
        "warn": "Running — something needs a look",
        "fail": "Something is not working",
    }[worst]
    rows = "\n".join(
        f"<tr><td><span class='pill' style='background:{_COLORS[r.level]}'>{_LABELS[r.level]}</span></td>"
        f"<td><b>{html.escape(r.name)}</b><div class='detail'>{html.escape(r.detail)}</div>"
        + (f"<div class='hint'>{html.escape(r.hint)}</div>" if r.level != "ok" and r.hint else "")
        + "</td></tr>"
        for r in results
    )
    checked = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>GB Automations</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 0; padding: 24px 16px; background: #f6f8fa; color: #1f2328; }}
  main {{ max-width: 760px; margin: 0 auto; }}
  h1 {{ font-size: 22px; margin: 0 0 4px; }}
  .banner {{ color: #fff; background: {_COLORS[worst]}; padding: 14px 16px; border-radius: 8px; font-size: 18px; margin: 16px 0; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; }}
  td {{ padding: 12px; border-bottom: 1px solid #eaeef2; vertical-align: top; }}
  td:first-child {{ width: 84px; }}
  .pill {{ color: #fff; border-radius: 999px; padding: 2px 10px; font-size: 13px; display: inline-block; }}
  .detail {{ color: #59636e; font-size: 14px; margin-top: 2px; overflow-wrap: anywhere; }}
  .hint {{ font-size: 14px; margin-top: 6px; overflow-wrap: anywhere; }}
  .meta {{ color: #59636e; font-size: 13px; margin-top: 12px; }}
</style></head>
<body><main>
<h1>GB Automations</h1>
<div class="banner">{headline}</div>
<table>{rows}</table>
<p class="meta">Checked {checked} &middot; refreshes every 30 s &middot; help: {GUIDE}</p>
</main></body></html>"""


@router.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    if request.headers.get("cf-connecting-ip"):
        raise HTTPException(404)
    return HTMLResponse(render_html(await run_checks()))
