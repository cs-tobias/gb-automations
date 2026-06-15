"""Test: first AsyncClient call in fresh process, vs after some prior."""

import asyncio
import os

import httpx


async def main() -> None:
    token = os.environ["FIKEN_API_TOKEN"]

    # Call A: fresh AsyncClient, first call
    async with httpx.AsyncClient(
        base_url="https://api.fiken.no/api/v2",
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r1 = await c.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "page": 1, "pageSize": 100},
        )
        print("A first call: body_len=", len(r1.text), "body[:80]=", repr(r1.text[:80]))

    # Call B: fresh AsyncClient AGAIN, first call
    async with httpx.AsyncClient(
        base_url="https://api.fiken.no/api/v2",
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r2 = await c.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "page": 1, "pageSize": 100},
        )
        print("B fresh client: body_len=", len(r2.text), "body[:80]=", repr(r2.text[:80]))

    # Call C: sync httpx.get
    r3 = httpx.get(
        "https://api.fiken.no/api/v2/companies/cinesuit-as/contacts",
        params={"customer": "true", "page": 1, "pageSize": 100},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
    )
    print("C sync get: body_len=", len(r3.text), "body[:80]=", repr(r3.text[:80]))


asyncio.run(main())
