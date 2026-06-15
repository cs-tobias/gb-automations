"""Show what's actually in the container's env + what a raw httpx call
returns with ONLY the bearer header (nothing else)."""

import os

import httpx


def main() -> None:
    token = os.environ.get("FIKEN_API_TOKEN", "")
    print("token present:", bool(token))
    print("token length:", len(token))
    print("token first 8 chars:", token[:8] if token else "<empty>")
    print("token last 4 chars:", token[-4:] if token else "<empty>")

    # Bare-minimum request — only Authorization, no Content-Type, no Accept.
    headers = {"Authorization": f"Bearer {token}"}
    r = httpx.get(
        "https://api.fiken.no/api/v2/companies/cinesuit-as/contacts",
        params={"customer": "true", "pageSize": 100},
        headers=headers,
        timeout=30.0,
    )
    print()
    print("status:", r.status_code)
    print("body length:", len(r.text))
    print("body[:300]:", repr(r.text[:300]))


main()
