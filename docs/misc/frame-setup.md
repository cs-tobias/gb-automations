# Frame.io setup (Phase 1: Notion → Frame mirror)

What this enables: every Notion Project row becomes its **own top-level Frame Project** under the configured workspace (visible in Frame V4's "Active Projects" view); every Task row becomes a subfolder per discipline with a placeholder file so the first delivery uploads as V2 on top. Renames in Notion propagate to Frame in place. Frame.io URLs are written back onto the Notion Project and Task rows.

The "Initialize" button on Projects fans out: it provisions Gmail labels + NAS folders + a Frame Project. Each target is an independent task in the durable queue, so a Frame outage can't block Gmail label retries.

## One-time setup

1. **Adobe Developer Console** — create an OAuth Web App credential with `offline_access` scope. Add the redirect URI `https://hub.<your-domain>/oauth/frame/callback` exactly as registered. Grab the Client ID + Client Secret. Put in `.env`:
   ```
   FRAME_CLIENT_ID=...
   FRAME_CLIENT_SECRET=...
   FRAME_REDIRECT_URI=https://hub.<your-domain>/oauth/frame/callback
   ```
2. **Run the bootstrap** to mint a refresh token and pick account + workspace:
   ```
   docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap
   ```
   Sign in as the shared studio account (e.g. `petter@goldbox.no`). The script prompts for an account + workspace, then shows you what's already in the workspace as a sanity check. Paste the printed values into `.env`:
   ```
   FRAME_REFRESH_TOKEN=...
   FRAME_ACCOUNT_ID=...
   FRAME_WORKSPACE_ID=...
   ```
   (There is no `FRAME_ROOT_PROJECT_ID` — each Notion project becomes its own Frame Project, created on demand. If a leftover value sits in `.env`, it's silently ignored.)
3. **Add the Frame.io URL property** to both the Projects DB and the Oppgaver (Tasks) DB in Notion. Type: **URL**. Default name: `Frame.io` — rename `PROJECTS_FRAME_URL_PROP` / `TASKS_FRAME_URL_PROP` in `config.py` if you choose a different column header.
4. **Flip the toggle** and restart the api:
   ```
   SYNC_FRAME=true
   FRAME_PLACEHOLDER_URL=https://hub.<your-domain>/assets/Goldbox_Logo_White.png
   # Optional: studio slot baked into each placeholder filename.
   # Default is "Goldbox.no" so this only needs setting if the studio
   # name changes.
   # FRAME_FILENAME_STUDIO=Goldbox.no
   ```
   ```
   docker compose up -d --force-recreate api
   ```
5. **Verify**:
   ```
   curl https://hub.<your-domain>/debug/frame              # whoami auth chain
   curl https://hub.<your-domain>/debug/frame/workspace    # lists projects in workspace
   curl https://hub.<your-domain>/assets/placeholder.png -o /tmp/p.png
   ```
   Then click **Initialize** on a fresh test Project in Notion. In Frame's web UI, open the workspace's Active Projects view; the new project appears as a top-level entry. Click into it: discipline subfolders nest inside. Add a task with Type=Eksteriør, click its button, expect `Eksteriør/<task name>/placeholder.png`. Both Notion rows should have their `Frame.io` URL populated.

## Layout

```
Frame.io workspace (FRAME_WORKSPACE_ID)
├─ 1234 Heimdal Solsletta bygg D     ← top-level Frame Project (FrameProjectFolder)
│  └─ (project's root_folder_id, auto-created)
│     ├─ Eksteriør/                  ← discipline folder, lazy
│     │  └─ Fasade Nord/             ← task folder (FrameTaskFolder)
│     │     └─ placeholder.png       ← FrameTaskFolder.frame_placeholder_file_id
│     ├─ Interiør/...
│     └─ Animasjon/...
├─ <ProjectName2>
│  └─ ...
```

Each Notion project becomes its own Frame Project entity (with its own `project_id`, its own `root_folder_id`, and its own active/inactive flag visible in Frame's UI). Discipline folder names live in `FRAME_DISCIPLINE_FOLDER_NAMES` (`config.py`); the placeholder file is the 69-byte 1×1 PNG checked into `src/gb_automations/assets/placeholder.png` and served by the FastAPI app over the Cloudflare tunnel.

The cache row stores BOTH the Frame Project id (`frame_project_id`) and its root folder id (`frame_folder_id`). The Project id is what we rename via `PATCH /projects/{id}` and what future active/inactive automation will toggle on; the root folder id is what discipline subfolders parent under.

## Operations

- **Status**: every Frame task lights the same Projects-DB status dot as the existing label/NAS flows — 🔄 active, ⚠️ retrying, 🛑 failed, ✅ idle. A Frame outage shows up as a yellow/red dot on the affected projects.
- **Queue visibility**: `GET /debug/queue` lists pending/in-progress/failed task counts and surfaces the last error per failed task; task types are `frame_project_sync` and `frame_task_sync`.
- **Retry a stuck task**: `docker compose exec api python -m gb_automations.scripts.retry_failed` flips every `failed` Frame task back to `pending`. Or click the Notion button again — idempotent.
- **Self-heal**: if you delete a Frame Project by hand, the next sync detects the stale id (a 404 on `get_project`), evicts the cache row, and recreates the Project from scratch. The cached placeholder file id is preserved across renames; cross-discipline moves are not yet implemented (a discipline change logs a warning and renames in place).
- **Adoption**: if a Frame Project with the matching name already exists in the workspace (e.g. someone created it by hand in Frame's UI before clicking Initialize), the sync ADOPTS it instead of creating a duplicate. Same adoption logic applies to discipline and task folders.

## Re-running the bootstrap

Re-run only if the refresh token is invalidated (Adobe password change, credential rotation) or you need to change account/workspace. The script clears stale state in `/tmp` before starting; pasted values overwrite the previous ones in `.env`.
