"""Test: does disabling gzip change the response?"""

import asyncio
import os

import httpx


async def main() -> None:
    token = os.environ["FIKEN_API_TOKEN"]

    # AsyncClient + accept-encoding: identity (no gzip)
    async with httpx.AsyncClient(
        base_url="https://api.fiken.no/api/v2",
        timeout=30.0,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept-Encoding": "identity",
        },
    ) as c:
        r = await c.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "page": 1, "pageSize": 100},
        )
        print("identity encoding:")
        print("  status:", r.status_code)
        print("  body len:", len(r.text))
        print("  response Content-Encoding:", r.headers.get("content-encoding"))
        print("  body[:120]:", repr(r.text[:120]))

    # AsyncClient default (gzip,deflate)
    async with httpx.AsyncClient(
        base_url="https://api.fiken.no/api/v2",
        timeout=30.0,
        headers={"Authorization": f"Bearer {token}"},
    ) as c:
        r = await c.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "page": 1, "pageSize": 100},
        )
        print()
        print("default encoding (gzip,deflate):")
        print("  status:", r.status_code)
        print("  body len:", len(r.text))
        print("  response Content-Encoding:", r.headers.get("content-encoding"))
        print("  raw bytes len:", len(r.content))
        print("  body[:120]:", repr(r.text[:120]))


asyncio.run(main())
