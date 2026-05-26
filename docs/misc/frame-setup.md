# Frame.io setup (Phase 1: Notion → Frame mirror)

What this enables: every Notion Project row becomes a folder under a shared Frame.io project; every Task row becomes a subfolder per discipline with a placeholder file so the first delivery uploads as V2 on top. Renames in Notion propagate to Frame in place. Frame.io URLs are written back onto the Notion Project and Task rows.

The "Sync to Gmail" button on Projects and Tasks fans out: it provisions Gmail labels + NAS folders + Frame folders. Each target is an independent task in the durable queue, so a Frame outage can't block Gmail label retries.

## One-time setup

1. **Adobe Developer Console** — create an OAuth Web App credential with `offline_access` scope. Add the redirect URI `https://hub.<your-domain>/oauth/frame/callback` exactly as registered. Grab the Client ID + Client Secret. Put in `.env`:
   ```
   FRAME_CLIENT_ID=...
   FRAME_CLIENT_SECRET=...
   FRAME_REDIRECT_URI=https://hub.<your-domain>/oauth/frame/callback
   ```
2. **Run the bootstrap** to mint a refresh token and pick account/workspace/root project:
   ```
   docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap
   ```
   Sign in as the shared studio account (e.g. `petter@goldbox.no`). The script prompts for an account, workspace, and the root Frame Project that will hold every Goldbox folder. Paste the printed values into `.env`:
   ```
   FRAME_REFRESH_TOKEN=...
   FRAME_ACCOUNT_ID=...
   FRAME_WORKSPACE_ID=...
   FRAME_ROOT_PROJECT_ID=...
   ```
3. **Add the Frame.io URL property** to both the Projects DB and the Oppgaver (Tasks) DB in Notion. Type: **URL**. Default name: `Frame.io` — rename `PROJECTS_FRAME_URL_PROP` / `TASKS_FRAME_URL_PROP` in `config.py` if you choose a different column header.
4. **Flip the toggle** and restart the api:
   ```
   SYNC_FRAME=true
   FRAME_PLACEHOLDER_URL=https://hub.<your-domain>/assets/placeholder.png
   ```
   ```
   docker compose up -d --force-recreate api
   ```
5. **Verify**:
   ```
   curl https://hub.<your-domain>/debug/frame         # whoami auth chain
   curl https://hub.<your-domain>/debug/frame/project # FRAME_ROOT_PROJECT_ID + root folder
   curl https://hub.<your-domain>/assets/placeholder.png -o /tmp/p.png
   ```
   Then click "Sync to Gmail" on a fresh test Project in Notion. In Frame, expect `<root project>/2026/<project name>/`. Add a task with Type=Eksteriør, click its button, expect `Eksteriør/<task name>/placeholder.png`. Both Notion rows should now have their `Frame.io` URL populated.

## Folder shape

```
Frame.io workspace
└─ Project FRAME_ROOT_PROJECT_ID (e.g. "Goldbox")
   └─ 2026/                                  ← year folder
      └─ 1234 Heimdal Solsletta bygg D/      ← project folder (FrameProjectFolder)
         ├─ Eksteriør/                       ← discipline folder, lazy
         │  └─ Fasade Nord/                  ← task folder (FrameTaskFolder)
         │     └─ placeholder.png            ← FrameTaskFolder.frame_placeholder_file_id
         ├─ Interiør/...
         └─ Animasjon/...
```

`(year, project leaf)` is byte-identical to the NAS folder and Gmail label leaves — same `utils.labels.project_path_parts` helper. A team member can match a NAS folder to its Frame folder visually without translating names.

Discipline folder names live in `FRAME_DISCIPLINE_FOLDER_NAMES` (`config.py`); the placeholder file is the 69-byte 1×1 PNG checked into `src/gb_automations/assets/placeholder.png` and served by the FastAPI app over the Cloudflare tunnel.

## Operations

- **Status**: every Frame task lights the same Projects-DB status dot as the existing label/NAS flows — 🔄 active, ⚠️ retrying, 🛑 failed, ✅ idle. A Frame outage shows up as a yellow/red dot on the affected projects.
- **Queue visibility**: `GET /debug/queue` lists pending/in-progress/failed task counts and surfaces the last error per failed task; new task types are `frame_project_sync` and `frame_task_sync`.
- **Retry a stuck task**: `docker compose exec api python -m gb_automations.scripts.retry_failed` flips every `failed` Frame task back to `pending`. Or click the Notion button again — idempotent.
- **Self-heal**: if you trash a Frame folder by hand, the next sync detects the stale id (a 404 on `get_folder`), evicts the cache row, and recreates the folder. The cached placeholder file id is preserved across renames; cross-discipline moves are not yet implemented (a discipline change logs a warning and renames in place).

## Re-running the bootstrap

Re-run only if the refresh token is invalidated (Adobe password change, credential rotation) or you need to change the root project. The script clears stale state in `/tmp` before starting; pasted values overwrite the previous ones in `.env`.
