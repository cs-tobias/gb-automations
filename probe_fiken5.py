"""Side-by-side: _client() vs manual AsyncClient. Dump every header
httpx actually sends so we can spot the difference."""

import asyncio
import os

import httpx

from gb_automations.clients import fiken


async def dump_request(label: str, send_call):
    print(f"\n=== {label} ===")
    r = await send_call()
    print(f"  request url: {r.request.url}")
    print(f"  request headers:")
    for k, v in r.request.headers.items():
        v_show = v if k.lower() != "authorization" else f"{v[:20]}...{v[-4:]}"
        print(f"    {k}: {v_show}")
    print(f"  response status: {r.status_code}")
    print(f"  response body len: {len(r.text)}")
    print(f"  response body[:120]: {r.text[:120]!r}")


async def main() -> None:
    token = os.environ["FIKEN_API_TOKEN"]

    # A) Our _client()
    async with await fiken._client() as c:
        await dump_request(
            "_client()",
            lambda: c.get(
                "/companies/cinesuit-as/contacts",
                params={"customer": "true", "page": 1, "pageSize": 100},
            ),
        )

    # B) Minimal AsyncClient, only Authorization
    async with httpx.AsyncClient(
        base_url="https://api.fiken.no/api/v2",
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        await dump_request(
            "minimal AsyncClient",
            lambda: c.get(
                "/companies/cinesuit-as/contacts",
                params={"customer": "true", "page": 1, "pageSize": 100},
            ),
        )


asyncio.run(main())
