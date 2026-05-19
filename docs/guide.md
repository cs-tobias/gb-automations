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

