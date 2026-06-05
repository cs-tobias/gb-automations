# NAS (office server) setup — project folders on the shared `W:` drive

When a project is created/renamed in Notion and the **Sync to Gmail** button is
clicked, the backend creates/renames a matching folder tree on Goldbox's office
NAS, alongside the Gmail label:

```
W:\Prosjekt\<year>\<full-project-name>\Mottatt\
e.g. W:\Prosjekt\2026\1187_Heimdal_Solsletta bygg D\Mottatt
```

`Mottatt` ("Received") is where incoming client/email files go. The project name
is the Notion title verbatim — identical to the Gmail label leaf and the Notion
title ("the name is this everywhere"). This works **only because the Docker host
is an office workstation on the same LAN as the NAS**, so the share is just a
mounted directory; there is no SMB client, VPN, or remote protocol in the code.

## One-time setup on the office host

Docker mounts the share itself as a CIFS volume — there is **no Windows-side
drive-letter mapping involved**. This sidesteps the WSL2-backend limitation
where bind-mounting a `W:` drive (or even a UNC path) into a container
silently lands an empty directory (see [gotchas.md §15](gotchas.md)).

1. **On Windows Docker Desktop (WSL2 backend): install `cifs-utils` in the
   `docker-desktop` distro.** One-time, from PowerShell:
   ```powershell
   wsl -d docker-desktop apk add cifs-utils
   ```
   May need to be re-run after a Docker Desktop major upgrade (the distro is
   sometimes wiped). Linux hosts already have it via the host kernel; skip
   this step.

2. **Set `.env`:**
   ```
   SYNC_NAS_FOLDERS=true
   NAS_CIFS_DEVICE=//192.168.1.200/filserver/gb-automations-test   # FORWARD slashes; the subfolder is the test root
   NAS_USER=petter
   NAS_PASS=<password for that share>
   NAS_PROJECTS_ROOT=/mnt/nas/Prosjekt                              # the Prosjekt root inside the share
   NAS_HOST_PATH=W:\gb-automations-test                             # Windows display path for Notion writeback (cosmetic)
   # NAS_RECEIVED_SUBFOLDER=Mottatt                                 # default; override only if Goldbox renames it
   ```
   `docker-compose.yml` declares the `nas` CIFS volume with these credentials.
   `NAS_HOST_PATH` is purely the display path written into the Projects-DB
   NAS URL column — it's never used as a mount source.

   For live (after the test sub-folder is validated), switch to:
   ```
   NAS_CIFS_DEVICE=//192.168.1.200/filserver
   NAS_HOST_PATH=W:
   # NAS_PROJECTS_ROOT stays /mnt/nas/Prosjekt
   ```

3. **Activate the prod overlay** by adding `COMPOSE_FILE` to `.env`. The
   separator is OS-specific — `;` on Windows, `:` on Linux/macOS:
   ```
   # Windows (prod is on Windows Docker Desktop):
   COMPOSE_FILE=docker-compose.yml;docker-compose.prod.yml
   # Linux/macOS would be:
   # COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
   ```
   `docker-compose.prod.yml` is a committed overlay that adds the
   `nas:/mnt/nas:rw` bind to the api service. Setting `COMPOSE_FILE` in
   `.env` makes `docker compose up -d` (no flags) auto-merge both files —
   so prod's command stays the same as everywhere else, the only
   difference is one line in `.env`. Dev boxes leave `COMPOSE_FILE` unset
   and get just the base file (no NAS bind), so the stack boots without
   any NAS configuration.

4. **Recreate the volume and the api container** (a `restart` alone won't
   re-mount the CIFS volume with the new options — Docker remembers the old
   options until the volume is removed):
   ```powershell
   docker compose down
   docker volume rm gb-automations_nas    # name = <project>_<volume>; check with `docker volume ls`
   docker compose up -d
   docker compose logs api | Select-String NAS
   ```
   Expect: `NAS project root '/mnt/nas/Prosjekt' is mounted and writable`. If
   you instead see the "is not a writable directory" warning, the mount or
   uid is wrong — fix it before clicking any project button (see
   [gotchas.md §15](gotchas.md)). A misconfigured NAS never blocks the
   Gmail-label step; it just reports `nas:failed` on each click.

   If the api container fails to **start** (not just warns) with a `mount
   error(13)` / `mount error(2)` / `Permission denied`, the CIFS mount
   itself is failing — bad credentials, wrong device path, wrong SMB
   version, or `cifs-utils` not installed in the docker-desktop distro
   (step 1). Check `docker compose logs api` for the mount error.

## Toggling targets while building

One Notion button → one webhook fans out to every enabled target, each switched
independently:

| Env var | Effect |
|---|---|
| `SYNC_GMAIL_LABELS` (default `true`) | Create/rename the Gmail label in every mailbox. Set `false` to test the NAS step without churning labels. |
| `SYNC_NAS_FOLDERS` (default `false`) | Create/rename the project folder on the NAS. |

The webhook JSON response reports each target: `{"action": <gmail>, "gmail": {…}, "nas": "created"|"renamed"|"unchanged"|"skipped"|"failed"}`.

## How it works (for maintainers)

- Folder naming: `utils/labels.py` `project_path_parts()` — the single source of
  the `<year>/<leaf>` scheme shared with the Gmail label, so the two can't drift.
- Filesystem ops: `clients/nas.py` (`ensure_project_folders`, `rename_project_folder`,
  `nas_available`) — sync, called via `asyncio.to_thread`.
- Mapping cache: `ProjectFolder` model / `project_folders` table — one row per
  project page, stores the last-written path/name so a Notion rename moves the
  folder in place. Migration `b5e3d2c0f147_add_project_folders`.
- Webhook wiring: `_sync_nas_folder_for_project()` in `routes/webhooks.py`,
  called from `_notion_webhook_impl` after the Gmail step.
