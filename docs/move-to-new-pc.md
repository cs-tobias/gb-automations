# Moving GB Automations to a new PC

The whole move is two double-clicks: **Export** on the old PC, **Install** on the new one. This page explains what those do, what's tied to a PC, and how to fix things when a step fails. It's written so Claude Code on the new PC can follow it, but a person can too.

> Setting up a brand-new deployment, with no old PC and new accounts? That's [guide.md](guide.md), not this page.

## The short version

**On the old PC** (the one running GB Automations now):

1. Double-click `deploy\windows\Export-ForMove.cmd` in the `gb-automations` folder.
2. Choose a password (8+ characters). You'll type it again on the new PC.
3. Wait. When it says **DONE**:
   - a folder `GB-Automations-Move-<date>` is on the Desktop;
   - GB Automations is switched **off** on this PC. Its data stays, as a backup.
4. Copy that folder to a USB stick.

**On the new PC** (Windows 10/11 Pro, on the office network, signed in with the account that will run it, which must be an administrator):

1. Copy the folder from the USB stick onto the PC (e.g. the Desktop). The installer runs as administrator, which can't see mapped network drives, and after a restart the USB stick may be gone.
2. Double-click `Install.cmd`, click **Yes** on the Windows prompt, and type the password.
3. If it says **RESTART NEEDED**: restart, sign in, and double-click `Install.cmd` again. It continues where it stopped.
4. When it says **INSTALLED**, the status page and the live log window open. Every status row should turn green within a few minutes. The first run downloads about 5 GB.
5. Do the **Left for a human** items it prints (see [After installing](#after-installing)).
6. Delete the move folder from the USB stick and from the new PC. It contains every password the system uses.

After that, nothing is needed day to day. GB Automations starts by itself whenever someone signs in to Windows, and opens a window with its live log. Docker starts in the background, with no Docker window. The **GB Automations** icon on the desktop does the same: it starts whatever isn't running, then shows the live log. Closing the log window never stops the app.

## Rules

- **Never run two PCs at once.** Both would connect to the same Cloudflare tunnel, and incoming webhooks would split between them. Both would run the hourly Fiken job, which double-adds `Fakturert beløp`. And Frame's sign-in, which renews daily, would fork. Export switches the old PC off for exactly this reason.
- **Never `docker compose down -v`.** `-v` deletes the database volume, which is the only copy of the state that can't be rebuilt (see below).
- **Never commit, email or chat the move folder, `.env` or `secrets/`.**
- **The app runs only while someone is signed in to Windows.** That's a Docker Desktop limitation. After a reboot (e.g. Windows Update) it's off until someone signs in. Windows automatic sign-in closes that gap but weakens the PC's security, so the client decides; the installer doesn't turn it on.
- **Docker Desktop is free only for companies under 250 employees AND under USD 10M revenue.** The installer accepts its license on that basis.

## What's tied to a PC (and what isn't)

Nothing is tied to the PC's hardware. No machine ID, hostname or MAC address is registered anywhere. Every outside service points at `https://hub.goldbox.no` or a Google Pub/Sub topic, never at a computer:

- Cloudflare tunnel
- Gmail push
- Frame webhook
- Adobe sign-in redirect
- every Notion button and automation

So a move needs no changes in any outside service. What does have to move:

| What | Where it lives | How it moves |
|---|---|---|
| Programs: Docker Desktop (with WSL2), Git, Ollama + the `llama3.1:8b-instruct-q4_K_M` model, `cifs-utils` in Docker's VM (for the NAS) | installed on the PC | Install installs them |
| Settings and every password/token | `.env` (not in git) | inside the move file |
| Google service-account key (can read every mailbox, via domain-wide delegation) | `secrets/gcp-service-account.json` (not in git) | inside the move file |
| The code, at the exact version the database expects | git | inside the move file (`git bundle`); `origin` then points at `https://github.com/cs-tobias/gb-automations.git` for future updates |
| **The database** | Docker volume `gb-automations_db_data` | inside the move file (`pg_dump`), restored before the app first starts |

The database matters most. It is **not** just a cache of Notion. These tables can't be rebuilt, and losing them causes real damage:

- **`oauth_tokens`**: the live Frame sign-in. It changes every day and expires after 14 days without a refresh. The `FRAME_REFRESH_TOKEN` in `.env` is an old seed and won't work.
- **`fiken_invoices`, `fiken_invoice_lines`, `faktura_notion_cache`**: the billing audit trail and its duplicate guards. Without them, invoice graduation breaks, duplicate Faktura rows appear, and double billing becomes possible.
- **`frame_comments`, `frame_leveranse_folders`**: without them, Frame events on existing comments create duplicate Korreksjon rows, and duplicate V00 placeholders can appear.
- **`attachment_blobs`, `email_content_dedup`**: without them, Drive attachments get re-uploaded and email rows get duplicated.
- **`sync_tasks`**: queued work, plus the record of what's already done.
- **`project_labels`**: without it, Gmail sync ignores every project until it's rebuilt.

## What the scripts do, step by step

All three scripts live in [`deploy/windows/`](../deploy/windows/). They are Windows PowerShell 5.1, ASCII-only, and log to `C:\ProgramData\gb-automations\<script>-<time>.log`. They never print secret values.

### Export-ForMove (old PC)

1. Checks `.env`, `secrets\gcp-service-account.json` and `.git` exist. It warns about uncommitted changes, because those won't move.
2. Starts Docker Desktop if needed. That also restarts the stack, which is fine.
3. Starts `db` and records a manifest:
   - commit, `alembic_version`
   - Frame token age (warns if over 12 days: plan a Frame sign-in)
   - row counts of the key tables
4. Runs `docker compose stop api cloudflared`, then counts again so the manifest matches the dump exactly. If anything fails from here on, it starts them again.
5. Dumps the database: `pg_dump -Fc` inside the container, copied out with `docker compose cp`. A PowerShell `>` redirect would corrupt the binary dump. It checks the dump with `pg_restore -l`.
6. Packs `.env`, the Google key, the dump, `repo.bundle` and `manifest.json` into a zip, then encrypts it to `payload.gbmove`:
   - key: PBKDF2-SHA256 from the password
   - cipher: AES-256-CBC, plus HMAC-SHA256 to catch a wrong password or damage

   It deletes the plaintext copy afterwards.
7. Writes the Desktop folder: `Install.cmd`, `Install.ps1`, `GbMove.Common.ps1`, `payload.gbmove`, `README.txt`.
8. Runs `docker compose down`, **without `-v`**, so nothing restarts on reboot and the volume stays as the rollback copy.

`-TestOnly` skips steps 4 and 8, so the app keeps running. Use it to make a test move file.

### Install (new PC)

Each step checks first whether it's already done, so re-running is always safe.

1. Re-launches itself as administrator, then decrypts the move file to `%TEMP%`. The decrypted copy is always deleted at the end, even on failure.
2. Checks:
   - virtualization is on;
   - `winget` exists (if not, install **App Installer** from the Microsoft Store);
   - the signed-in Windows account matches the installer's account.
3. Turns on the Windows features `Microsoft-Windows-Subsystem-Linux` and `VirtualMachinePlatform`.
4. Installs with winget, skipping anything present. If winget can't run, it downloads each installer directly from its maker and checks the signature before running it:
   - Git;
   - Docker Desktop (silent, license accepted, WSL2 backend);
   - Ollama.

   Then it adds the user to `docker-users`. If any of this needs a restart, it says **RESTART NEEDED** and stops.
5. Runs `wsl --update` and starts Docker Desktop.
6. Sets up Ollama:
   - sets the user variable `OLLAMA_HOST=0.0.0.0:11434`, so containers can reach it via `host.docker.internal`;
   - adds the firewall rule **GB Automations - Ollama**, limited to the Docker/WSL address ranges;
   - restarts Ollama and pulls the model.
7. If `SYNC_NAS_FOLDERS=true`:
   - checks the NAS host from `NAS_CIFS_DEVICE` answers on port 445. The PC must be on the office network.
   - installs `cifs-utils` in the `docker-desktop` WSL distro.
8. Sets sleep and hibernate on mains power to never.
9. Sets up the code:
   - clones `repo.bundle` to `C:\Automatisering\gb-automations` and checks out the manifest's commit;
   - points `origin` at GitHub.

   The folder must be named `gb-automations`: the Docker volume names come from it.
10. Places `.env` and `secrets\gcp-service-account.json`. It never overwrites a different existing file; it warns instead. It then makes the signed-in user the owner of the folder.
11. Runs `docker compose up -d db`.
    - If the database is empty, it restores the dump and checks every key table's row count against the manifest.
    - If it already has tables, it is **left untouched**.

    The api must not start before this. Its first boot would run migrations on the empty database and the restore would collide.
12. Runs `docker compose up -d --build` and waits for `/health` to report `db: ok`.

    The installer runs no separate email catch-up. Every app start lists the threads under each project label in Gmail and queues only the ones the database has never synced (`_recover_and_reconcile_queue` in `main.py`). That covers email labelled while no PC was running. `scripts.reconcile` re-syncs every thread of every project through Notion. It takes hours, hits Notion's rate limit, and grows with every project, so keep it for repairs.
13. Creates two shortcuts:
    - the **GB Automations** desktop icon;
    - **GB Automations (autostart)** in the Startup folder.

    Both open the same window, described below. Install then opens the status page and that window.

`-RestoreTestOnly` decrypts the move file and restores the dump into a throwaway `postgres:16-alpine` container. It compares migration level and row counts, then deletes the container. It installs nothing and doesn't touch a running app, so it's safe on the old PC too. `GBMOVE_PASSWORD` (environment variable) and `-NoPause` exist so the test can run unattended.

### Start-GbAutomations (the desktop icon and the sign-in autostart)

One window that does this:

1. Starts Ollama if it isn't answering.
2. Starts Docker Desktop in the background if it's closed. Every run, it sets Docker's *Open Docker Dashboard when Docker Desktop starts* setting to off (`OpenUIOnStartupDisabled` in `%APPDATA%\Docker\settings-store.json`), so no Docker window appears. It never launches Docker a second time while Docker is still starting.
3. Makes sure `cifs-utils` is still in Docker's VM. A Docker Desktop upgrade can drop it.
4. Runs `docker compose up -d`. It never rebuilds.
5. Waits up to 3 minutes for `/health`.
6. Becomes the **live log**: `docker compose logs --follow api`, under the title *GB Automations - live log*.
   - If the app restarts (an update, a crash, Docker restarting), the log carries on where it stopped.
   - If the app didn't answer in step 5, it says so and shows the log anyway, since the log shows why.
   - Clicking the icon while a live log window is open switches to that window instead of opening a second one.
   - Closing the window only closes the view. The app keeps running.

If step 1–4 fails, the window shows the problem in red and stays open until Enter. Each start is logged to `C:\ProgramData\gb-automations\start-*.log`, but the live log itself isn't. It's always in `docker compose logs api`.

Installs from before 2026-11-13 have a hidden sign-in shortcut that passes `-AtLogon`. The script answers that by reopening itself in a normal window, so old shortcuts get the live log too.

### The status page

`http://localhost:8000/status` ([routes/status.py](../src/gb_automations/routes/status.py)) shows one row per integration:

- Database
- Gmail (Google key and delegation)
- Notion
- AI tagging
- Work queue
- Notion automations
- NAS, Frame, Fiken and Toggl, when enabled

Green is fine. Amber needs a look. Red is broken, and comes with a one-line fix. The page refreshes every 30 s. It only answers on the PC itself: requests through the Cloudflare tunnel get a 404.

## After installing

- **Notion automations.** Notion pauses an automation once its webhook fails (the threshold isn't published), which happens while no PC is running. Open the Automations panel on the **Projects**, **Oppgaver** and **Korreksjoner** databases and switch back on any marked as paused. Notion has no API for this. Then click a Notion button once. The *Notion automations* row on the status page should show it. Details: [misc/notion-setup.md](misc/notion-setup.md) Part 6e.
- **Frame**, only if its row is red, or Install said Frame needs a sign-in. Run this **with only the new PC running**:

  ```powershell
  cd C:\Automatisering\gb-automations
  docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap
  ```

  Open the printed link and sign in as the studio's Frame account. Adobe sends the browser back to `hub.goldbox.no`, so it must reach this PC. The new sign-in is stored in the database; nothing to paste. Details: [misc/frame-setup.md](misc/frame-setup.md).
- **Smoke test.** Put a project label on a test email in Gmail. Within about a minute it should appear in Notion, and the *Work queue* row should show nothing waiting.

## Troubleshooting

Check the log first: `C:\ProgramData\gb-automations\install-*.log` (or `export-*`, `start-*`). For integration issues, read [misc/gotchas.md](misc/gotchas.md) next; most failures already have an entry.

| Symptom | Fix |
|---|---|
| "Could not start winget" / "Kan ikke starte programmet" | Handled automatically: Install re-registers winget's App Installer package for the account, and if winget still won't run it downloads Docker Desktop, Ollama and Git straight from their makers (each installer's signature is checked first). A move folder made **before 2026-11-13** has the old installer: replace its `Install.ps1` and `GbMove.Common.ps1` with the current ones from `deploy\windows\`. |
| Install says **RESTART NEEDED** every time | Check that **Virtual Machine Platform** and **Windows Subsystem for Linux** show as on in "Turn Windows features on or off". If they won't turn on, enable virtualization (Intel VT-x / AMD SVM) in the BIOS. |
| "Docker Desktop did not finish starting" | Open Docker Desktop from the Start menu and read what it says. Usually it's a first-run screen, or WSL needs `wsl --update`. When it shows *Engine running*, run Install/the icon again. |
| "Can't reach the NAS" / `mount error(13)` / `mount error(2)` / "no such file or directory" / app container won't start | **One bad NAS mount stops the whole app, not just NAS folders.** The PC must be on the office network. The **folder** in `NAS_CIFS_DEVICE` must still exist on the NAS: a renamed or deleted folder gives "no such file or directory" (`cifs_mount failed w/return code = -2` in `wsl -d docker-desktop dmesg`). `NAS_USER` and `NAS_PASS` must be right (wrong ones give error 13). Docker caches the NAS mount options, so after changing them run `docker compose down`, then `docker volume rm gb-automations_nas`, then `docker compose up -d`. See [misc/gotchas.md](misc/gotchas.md) §15 and [misc/nas-setup.md](misc/nas-setup.md). |
| AI tagging row red/amber | Start **Ollama** from the Start menu. If the model is missing: `ollama pull llama3.1:8b-instruct-q4_K_M`. If Ollama runs but the row stays red, check the firewall rule **GB Automations - Ollama** exists. |
| Frame row red (`invalid_grant`, 401) | The Frame sign-in expired. Run `frame_oauth_bootstrap` (see [After installing](#after-installing)). |
| Gmail row red | `secrets\gcp-service-account.json` is missing, or `SYNCED_MAILBOXES` in `.env` is empty. Delegation is tied to the key, not the PC, so it doesn't need redoing. |
| "Restored database does not match the old PC" | The move file is damaged, or was made while the app was still writing. Make a new one on the old PC (normal mode, not `-TestOnly`). |
| "This PC already has a GB Automations database" | Install never overwrites a database. If this PC's database is a leftover you really want replaced: `docker compose down`, then `docker volume rm gb-automations_db_data`, then run Install again. **This deletes that data.** |
| Edited `.env` but nothing changed | `docker compose up -d --force-recreate api`. A plain restart doesn't reload `.env` ([misc/gotchas.md](misc/gotchas.md) §1). |
| Status page shows "Work queue: N failed" | Fix the cause (the row shows it), then `Invoke-RestMethod -Method Post http://localhost:8000/debug/queue/retry-failed`. |

## Rolling back

If the new PC misbehaves, first switch the new PC off:

```powershell
cd C:\Automatisering\gb-automations; docker compose down
```

Then switch the old one back on:

```powershell
cd <its gb-automations folder>; docker compose up -d
```

Do this **only before** the new PC has done real work. Anything it synced or invoiced in the meantime exists only in its database. After that, the way back is a fresh Export from the new PC, installed on the old one.

When the new PC has run fine for a couple of weeks, clean up the old PC by running `docker compose down -v` there. Only there, and only then: it deletes the old copy of the data. Then delete its `.env` and `secrets\`.

## If the old PC is dead (no move file)

Without the old database this path loses data, so recover the old disk if at all possible. Otherwise:

1. **Get `.env` and the Google key from a backup:** a password manager, or the old disk's `gb-automations\.env` and `gb-automations\secrets\`. Keeping a copy of both in the client's password manager makes this step painless. With no backup, regenerate each secret:
   - Notion token: Notion integrations page.
   - Tunnel token: Cloudflare Zero Trust, then the tunnel's connector.
   - Google key: a **new key on the same service account**. Delegation stays valid.
   - Fiken and Toggl tokens: their settings pages.
   - A new `NOTION_WEBHOOK_SECRET` has to be pasted into **every** Notion button and automation.
   - A new `FRAME_WEBHOOK_SECRET` comes from `python -m gb_automations.scripts.frame_register_webhook`.
2. Install the programs and code by hand, following the Install steps above.
3. Set `SYNC_FIKEN_GRADUATIONS=false` in `.env` **before the first start**. The hourly Fiken job would otherwise write duplicate Faktura rows with no audit trail.
4. Start with `docker compose up -d --build`. Then run these, in this order:
   - `docker compose exec api python -m gb_automations.scripts.backfill_project_labels`
   - `docker compose exec api python -m gb_automations.scripts.reconcile`
   - `docker compose exec api python -m gb_automations.scripts.frame_oauth_bootstrap`
5. Expect these losses:
   - Fiken: drafts and invoices sent before the move no longer graduate statuses automatically.
   - Frame: updates to old comments can create duplicate Korreksjon rows.
   - Drive: attachments get re-uploaded when old threads re-sync.
   - NAS: a project renamed later gets a new folder instead of having its old one renamed.

## If the public address ever changes

It doesn't change in a move. If `hub.goldbox.no` itself changes, update:

- the Cloudflare public hostname;
- the Pub/Sub push endpoint and `PUBSUB_AUDIENCE`;
- the Frame webhook: run `frame_register_webhook`, then delete the old one by hand;
- `FRAME_REDIRECT_URI` in `.env` and the Adobe console;
- `FRAME_PLACEHOLDER_URL`;
- the URL in every Notion button and automation.
