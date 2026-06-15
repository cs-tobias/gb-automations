"""Dump full response headers + try a different endpoint to see if it's
endpoint-specific or token-wide."""

import os
import httpx

token = os.environ["FIKEN_API_TOKEN"]
headers = {"Authorization": f"Bearer {token}"}

# /contacts
r = httpx.get(
    "https://api.fiken.no/api/v2/companies/cinesuit-as/contacts",
    params={"customer": "true", "pageSize": 100},
    headers=headers,
    timeout=30.0,
)
print("=== /contacts ===")
print("  status:", r.status_code, "  body_len:", len(r.text))
print("  ALL response headers:")
for k, v in r.headers.items():
    print(f"    {k}: {v}")

# /companies
r = httpx.get(
    "https://api.fiken.no/api/v2/companies",
    headers=headers,
    timeout=30.0,
)
print()
print("=== /companies ===")
print("  status:", r.status_code, "  body_len:", len(r.text))
print("  body[:200]:", repr(r.text[:200]))

# /user
r = httpx.get(
    "https://api.fiken.no/api/v2/user",
    headers=headers,
    timeout=30.0,
)
print()
print("=== /user ===")
print("  status:", r.status_code, "  body_len:", len(r.text))
print("  body[:200]:", repr(r.text[:200]))
