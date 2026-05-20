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

1. **Mount the share** so the container's user can write to it.
   - Windows Docker Desktop host: ensure the mapped `W:` drive (or its UNC path)
     is shared with Docker Desktop (Settings → Resources → File sharing) and set
     `NAS_HOST_PATH` to the host path Docker can see.
   - Linux host (CIFS): mount with options that match the container user, e.g.
     ```bash
     mount -t cifs //srvr-2/Prosjekt /mnt/nas/Prosjekt \
       -o username=...,password=...,uid=<container-uid>,gid=<container-gid>,file_mode=0664,dir_mode=0775
     ```
     See [gotchas.md #15](gotchas.md) — the uid/gid mismatch is the classic
     failure where the host can write but the container can't.

2. **Set `.env`:**
   ```
   SYNC_NAS_FOLDERS=true
   NAS_HOST_PATH=<host path to the mounted share root>   # bound into the container at /mnt/nas
   NAS_PROJECTS_ROOT=/mnt/nas/Prosjekt                   # the Prosjekt root inside that mount
   # NAS_RECEIVED_SUBFOLDER=Mottatt                      # default; override only if Goldbox renames it
   ```
   `docker-compose.yml` binds `${NAS_HOST_PATH}:/mnt/nas` on the `api` service.

3. **Reload and verify the green light:**
   ```bash
   docker compose up -d --force-recreate api
   docker compose logs api | grep NAS
   ```
   Expect: `NAS project root '/mnt/nas/Prosjekt' is mounted and writable`. If you
   instead see the "is not a writable directory" warning, the mount or uid is
   wrong — fix it before clicking any project button (see gotchas #15). A
   misconfigured NAS never blocks the Gmail-label step; it just reports
   `nas:failed` on each click.

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
