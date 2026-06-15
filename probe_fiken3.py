"""Bisect: which header combo breaks Fiken /contacts?"""

import os

import httpx


def call(label: str, headers: dict[str, str]) -> None:
    r = httpx.get(
        "https://api.fiken.no/api/v2/companies/cinesuit-as/contacts",
        params={"customer": "true", "pageSize": 100},
        headers=headers,
        timeout=30.0,
    )
    print(f"{label:50s}  status={r.status_code}  body_len={len(r.text)}")


def main() -> None:
    token = os.environ["FIKEN_API_TOKEN"]
    auth = {"Authorization": f"Bearer {token}"}

    call("(1) auth only", auth)
    call("(2) auth + Accept",       {**auth, "Accept": "application/json"})
    call("(3) auth + Content-Type", {**auth, "Content-Type": "application/json"})
    call("(4) auth + Accept + Content-Type", {
        **auth,
        "Accept": "application/json",
        "Content-Type": "application/json",
    })


main()
