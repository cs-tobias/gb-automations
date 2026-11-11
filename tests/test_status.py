"""`GET /status` — the local-only operator page behind the desktop icon.

Pins three contracts: tunneled requests (Cloudflare adds `Cf-Connecting-Ip`)
get a 404, every check renders as its own row with the right severity, and a
hung or crashing check can't take the page down with it.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

import gb_automations.routes.status as status


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(status.router)
    return app


async def _get(headers: dict[str, str] | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        return await client.get("/status", headers=headers or {})


async def _ok() -> str:
    return "all good"


async def _warn() -> str:
    raise status.CheckWarning("needs a look")


async def _boom() -> str:
    raise RuntimeError("token revoked")


async def _hang() -> str:
    await asyncio.sleep(60)
    return "never"


@pytest.fixture
def checks(monkeypatch):
    def install(*items: status.Check) -> None:
        monkeypatch.setattr(status, "_checks", lambda: list(items))

    return install


async def test_tunneled_request_is_hidden(checks):
    checks(status.Check("Database", _ok, ""))
    resp = await _get({"Cf-Connecting-Ip": "203.0.113.7"})
    assert resp.status_code == 404


async def test_all_green(checks):
    checks(status.Check("Database", _ok, "hint-db"), status.Check("Notion", _ok, "hint-notion"))
    resp = await _get()
    assert resp.status_code == 200
    assert "Everything is running" in resp.text
    assert resp.text.count("all good") == 2
    assert "hint-db" not in resp.text


async def test_failure_shows_hint_and_red_banner(checks):
    checks(
        status.Check("Database", _ok, "hint-db"),
        status.Check("Frame.io", _boom, "run frame_oauth_bootstrap"),
    )
    resp = await _get()
    assert resp.status_code == 200
    assert "Something is not working" in resp.text
    assert "token revoked" in resp.text
    assert "run frame_oauth_bootstrap" in resp.text
    assert "hint-db" not in resp.text


async def test_warning_is_amber_not_red(checks):
    checks(status.Check("Work queue", _warn, "retry hint"))
    resp = await _get()
    assert "something needs a look" in resp.text
    assert "needs a look" in resp.text
    assert "retry hint" in resp.text


async def test_hung_check_times_out_without_blocking_others(checks, monkeypatch):
    monkeypatch.setattr(status, "CHECK_TIMEOUT_S", 0.05)
    checks(status.Check("Ollama", _hang, "start ollama"), status.Check("Database", _ok, ""))
    resp = await _get()
    assert "no answer within" in resp.text
    assert "all good" in resp.text


async def test_detail_is_html_escaped(checks):
    async def _evil() -> str:
        raise RuntimeError("<script>alert(1)</script>")

    checks(status.Check("Notion", _evil, ""))
    resp = await _get()
    assert "<script>" not in resp.text
    assert "&lt;script&gt;" in resp.text
