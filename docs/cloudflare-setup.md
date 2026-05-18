# Cloudflare setup

All clicks, no script. About 15 minutes plus waiting time for nameserver activation.

---

## Part 1 — Add the domain

1. Open https://dash.cloudflare.com and sign in
2. Click **Add a Site**, enter your domain (e.g. `goldbox.no`), pick **Free** plan
3. Review the imported DNS records — **check that MX records (Google Workspace) are there** before continuing, or email breaks
4. Cloudflare shows you two nameservers. Go to your domain registrar (wherever you bought the domain) and replace the nameservers with those two
5. Wait for the activation email from Cloudflare (usually a few minutes, sometimes hours)

---

## Part 2 — Delete wildcard records

After activation, look at the DNS records list.

1. Find any **A** record where the name is `*` (wildcard)
2. Delete them all

This avoids a known gotcha where wildcards hijack the tunnel subdomain.

---

## Part 3 — Create the tunnel

1. Go to **Zero Trust → Networks → Tunnels** → **Create a tunnel**
2. Pick **Cloudflared** as the connector type
3. Name it `gb-automations-prod`
4. Copy the **token** (a long `eyJ...` string) — save it, you'll paste it into `.env` later
5. On the **Public Hostnames** step, click **Add a public hostname**:
   - Subdomain: `hub`
   - Domain: your domain (e.g. `goldbox.no`)
   - Type: `HTTP`
   - URL: `api:8000`
6. Save

---

Cloudflare is done.
