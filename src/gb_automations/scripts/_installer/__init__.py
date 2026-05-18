"""Host-side installer package for gb-automations.

Unlike the rest of `gb_automations`, modules in this package run on the *host*
machine (typically the operator's Mac or the office PC) — not inside the api
docker container. They shell out to `gcloud`, `cloudflared`, `docker compose`,
write files to the host filesystem (`.env`, `secrets/gcp-service-account.json`,
`.setup_state.json`), and only use Python stdlib so the installer can run
before the project's uv environment is set up.
"""
