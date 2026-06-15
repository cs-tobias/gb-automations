"""One-shot diagnostic: hit Fiken /contacts directly via the same client
list_contacts uses, and dump status, URL, body length, parsed shape."""

import asyncio

from gb_automations.clients import fiken


async def main() -> None:
    async with await fiken._client() as client:
        r = await client.get(
            "/companies/cinesuit-as/contacts",
            params={"customer": "true", "page": 1, "pageSize": 100},
        )
        print("status:", r.status_code)
        print("url:", r.request.url)
        print("body length:", len(r.text))
        print("body[:200]:", repr(r.text[:200]))
        try:
            j = r.json()
            print("json type:", type(j).__name__)
            if isinstance(j, list):
                print("json len:", len(j))
                if j:
                    first = j[0]
                    print("first keys:", list(first.keys())[:8])
            elif isinstance(j, dict):
                print("json keys:", list(j.keys())[:10])
        except Exception as e:
            print("json parse error:", e)

    # Now also call list_contacts itself and see what comes back.
    contacts = await fiken.list_contacts("cinesuit-as", customer=True)
    print("list_contacts returned:", len(contacts), "contacts")


asyncio.run(main())
