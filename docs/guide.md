1. winget install Docker.DockerDesktop
winget install Git.Git
winget install Microsoft.VisualStudioCode

2. open terminal, cd into the dir we want, then: git clone https://github.com/cs-tobias/gb-automations.git
populate .env with .example+actual data.

3. google cloud console /scripts/gcp-bootstrap.sh, then DWD setup (follow prompt in google shell).

4. https://dash.cloudflare.com sign up. "domains -> +" (add site, connect domain). goldbox.no... free... Make sure dns records match what you have in one.com (also sign into one.com).
Set up nameservers to cloudlfare.
Remove "any **A** record where the name is `*` (wildcard)"

5. Zero trust - networks - connectors - "create a tunnel" - cloudflared - name "gb-automations-prod" - copy "eyJ" token into .env
On the **Public Hostnames** step, click **Add a public hostname**:
   - Subdomain: `hub`
   - Domain: your domain (e.g. `goldbox.no`)
   - Type: `HTTP`
   - URL: `api:8000`

6. https://www.notion.so/profile/integrations -> new integration -> "Copy the **Internal Integration Secret** (starts with `ntn_…`) → paste into `.env` as `NOTION_TOKEN`"
7. Set `NOTION_WEBHOOK_SECRET` in `.env` to a long random string (`openssl rand -hex 32`); reload api.
8. Add the **Sync to Gmail** button to the Projects DB per [docs/notion-setup.md](notion-setup.md) Part 4 — it POSTs to `/webhooks/notion` with that secret as a bearer token on each click.
9. Fresh / migrated host — rebuild the per-machine cache. The `project_labels` map (which Gmail labels are projects) lives only in this host's Postgres volume, so a new host is blind until it's rebuilt. After `seed_users` + `start_watches`, run:
   - `docker compose exec api python -m gb_automations.scripts.backfill_project_labels` — reconstructs `project_labels` from existing Notion projects + Gmail labels (no labels created, nothing in Notion touched). Add `--dry-run` to preview. Projects it lists as having "no Gmail label yet" still need a one-time **Sync to Gmail** click.
   - `docker compose exec api python -m gb_automations.scripts.reconcile` — syncs any threads sitting under a project label that Notion never received (e.g. labeled while the host was down). Idempotent; run it after any downtime, not just on a fresh host.

10. (Optional) Office NAS project folders: mount the shared `W:` drive into the container and set `SYNC_NAS_FOLDERS=true` per [docs/misc/nas-setup.md](misc/nas-setup.md). Leave `SYNC_NAS_FOLDERS=false` to skip.

11. (Optional) Frame.io mirror: run `docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap`, paste the printed values into `.env`, then set `SYNC_FRAME=true` and `FRAME_PLACEHOLDER_URL=https://hub.<your-domain>/assets/placeholder.png` and `docker compose up -d --force-recreate api`. The "Sync to Gmail" button now also provisions a Frame folder per project + per task (with a placeholder file). Verify with `curl https://hub.<your-domain>/debug/frame/project`. Full notes in [docs/misc/frame-setup.md](misc/frame-setup.md).

