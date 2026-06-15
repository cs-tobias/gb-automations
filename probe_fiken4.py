"""Isolate the AsyncClient + base_url path."""

import asyncio
import os

import httpx


async def call(label: str, **client_kwargs) -> None:
    async with httpx.AsyncClient(**client_kwargs, timeout=30.0) as c:
        r = await c.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "pageSize": 100},
        )
        print(f"{label:50s}  url={r.request.url}  status={r.status_code}  body_len={len(r.text)}")


async def main() -> None:
    token = os.environ["FIKEN_API_TOKEN"]
    auth = {"Authorization": f"Bearer {token}"}

    await call("(A) AsyncClient w/ base_url no trailing slash",
               base_url="https://api.fiken.no/api/v2", headers=auth)
    await call("(B) AsyncClient w/ base_url WITH trailing slash",
               base_url="https://api.fiken.no/api/v2/", headers=auth)


asyncio.run(main())
