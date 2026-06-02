# Toggl Track — production setup guide

This is the step-by-step guide for turning on the Toggl integration against **Goldbox's** Toggl workspace and Notion workspace. Run through it once; the nightly cron takes over afterwards.

---

## Prerequisites

- The api container is running and healthy (`docker compose up -d --build`)
- You have access to the Goldbox Toggl workspace as an **admin** (you need admin to read other people's time entries via Reports v3)
- You have access to the Goldbox Notion workspace with the gb-automations integration already connected

---

## Step 1 — Get the Toggl API token

1. Sign in to Toggl Track as the **Goldbox admin account**: https://track.toggl.com
2. Go to **Profile settings**: https://track.toggl.com/profile
3. Scroll to the bottom → **API Token** section → click **Click to reveal**
4. Copy the 32-character hex string

Add it to `.env` on the server:

```
TOGGL_API_TOKEN=<paste token here>
```

---

## Step 2 — Run the bootstrap script

This finds the workspace ID and lists all team members. Run inside the container:

```powershell
docker compose exec api python -m gb_automations.scripts.toggl_bootstrap
```

The script will print something like:

```
Authenticated as: Goldbox Admin <admin@goldbox.no>

Single workspace found: 'Goldbox' (1234567) — using it.

Found 5 member(s):
  toggl_user_id  email                               name
  -------------- ----------------------------------- ------------------------------
  9876543        ola@goldbox.no                      Ola Nordmann (admin)
  9876544        kari@goldbox.no                     Kari Nordmann
  ...

DONE — paste this into .env:

TOGGL_WORKSPACE_ID=1234567
```

Add `TOGGL_WORKSPACE_ID` to `.env`. Also **verify that every team member's email in the list matches their Notion login email** — the hours sync attributes rows by email match, so a mismatch means that person's hours won't appear in Notion.

---

## Step 3 — Create the Timer parent page in Notion

The integration auto-creates `Timer 2026`, `Timer 2027`, etc. databases — but it needs a parent page to put them under.

1. In the Goldbox Notion workspace, create a new page where you want the Timer databases to live (e.g. inside the main workspace or alongside the other integration DBs)
2. Share that page with the **gb-automations** Notion integration (click Share → invite the integration)
3. Copy the page ID from the URL: `https://notion.so/<workspace>/<page-id-here>?...` — it's the 32-char hex string in the path
4. Add to `.env`:

```
TOGGL_TIMER_PARENT_PAGE_ID=<page id here>
```

---

## Step 4 — Enable the integration

Add/update these lines in `.env`:

```
SYNC_TOGGL=true
SYNC_TOGGL_HOURS=true
```

Leave `TOGGL_DEV_EMAIL_OVERRIDES` blank — Goldbox's Toggl and Notion accounts share the same email addresses, so no override is needed.

---

## Step 5 — Restart the api

```powershell
docker compose up -d --force-recreate api
```

---

## Step 6 — Verify user matching

Check that all team members are found and matched:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/debug/toggl/refresh-users
```

Look at the response:

- `toggl_users_cached` — should equal the number of team members from the bootstrap output
- `notion_users_indexed_by_email` — should equal the number of Notion workspace members

If the counts match, every person's hours will be attributed correctly. If `toggl_users_cached` is higher than expected, check for ex-employees still in the Toggl workspace.

---

## Step 7 — Mirror all projects to Toggl

Run this once to enqueue a project sync for every row in the Notion Projects DB:

```powershell
Invoke-RestMethod -Method Post http://localhost:8000/debug/toggl/sync-all-projects
```

The response shows how many were enqueued. The worker then processes them in the background — each project gets either created in Toggl or adopted if a same-name project already exists, and the Toggl URL is written back to the Notion row.

**Order matters but isn't strict:** the hours engine writes every Toggl entry to Notion regardless — for entries whose Toggl project isn't in the cache yet, the row lands with an empty `Prosjekt` relation (and the Toggl project name stored in `Toggl Prosjekt navn`). Running this step before backfill means more rows will have the relation set on first creation. If you skip this step, those rows still exist; the relation just gets filled in on the next sync once the cache catches up.

Watch the logs to confirm it drains:

```powershell
docker compose logs -f api | grep -v "GET /health"
```

Going forward, new projects get their Toggl mirror created automatically when you press the **Sync Toggl** or **Initialize** button on the Notion row.

---

## Step 8 — Historical backfill

Pull all Toggl entries from January 1st through today and write them to Notion:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/debug/toggl/backfill"
```

This defaults to `from=Jan 1 of current year, to=today`. For a custom range:

```powershell
Invoke-RestMethod -Method Post "http://localhost:8000/debug/toggl/backfill?from=2026-01-01&to=2026-06-02"
```

The response shows how many rows were created/updated. This may take a minute for a full year of data. It creates the `Timer 2026` database automatically under the parent page you set in step 3.

---

## Step 9 — Verify in Notion

Open the `Timer 2026` database. You should see rows with:
- **Navn** — title like "Ola Nordmann — 2026-05-15"
- **Dato** — the calendar date
- **Ansatt** — the team member (Notion people field, clickable)
- **Prosjekt** — the Notion project relation
- **Timer** — decimal hours (e.g. 7.5)

---

## Ongoing operation

From here on, no manual steps are needed:

- **Nightly at 02:00 Oslo** — the scheduler enqueues a `toggl_hours_sync` task that re-checks the last 32 days and writes any new or edited entries to Notion
- **New projects** — press the Sync Toggl (or Initialize) button on a new Notion project row to create the Toggl project
- **Renamed projects** — pressing Sync Toggl again renames the Toggl project to match Notion
- **Queue status** — check `GET /debug/queue` if something looks stuck

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Hours not appearing for one person | Their Toggl email doesn't match their Notion login email — check the bootstrap output vs. Notion members |
| A project's hours not appearing | The project wasn't synced to Toggl yet — press Sync Toggl on the Notion row, then re-run backfill for that period |
| `toggl_users_cached` is 0 after refresh | `TOGGL_API_TOKEN` or `TOGGL_WORKSPACE_ID` is wrong — re-run bootstrap |
| Backfill returns `action: skipped` | `SYNC_TOGGL_HOURS=false` — check `.env` and force-recreate |
| Entries show wrong date (off by one) | Timezone issue — all dates are bucketed in Europe/Oslo; entries recorded near midnight may land on the previous or next day |
