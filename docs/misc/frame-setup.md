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
3. **Add the Frame.io URL property** to both the Projects DB and the Oppgaver DB in Notion. Type: **URL**. Default name: `Frame.io` — rename `PROJECTS_FRAME_URL_PROP` / `OPPGAVER_FRAME_URL_PROP` in `config.py` if you choose a different column header.
   **Add `Klargjøre modell` to the `Type` select** on the Oppgaver DB (alongside Interiør/Eksteriør/Animasjon/Annet). Deliverable-vs-internal is decided by `Type` alone: a real discipline gets Frame + NAS provisioning when the Sync button is clicked; `Klargjøre modell` (or any other non-discipline value, or a blank Type) is treated as an internal task and skipped. There is NO separate Kategori property. Set `OPPGAVER_DB_ID` (the deliverables/tasks DB) and `KORREKSJONER_DB_ID` (the feedback-items DB) in `.env`.
4. **Flip the toggle** and restart the api:
   ```
   SYNC_FRAME=true
   # Static fallback placeholder + the origin the DYNAMIC placeholder endpoint
   # is derived from (scheme+host only). The V00 placeholder is normally
   # rendered per-deliverable at /assets/placeholder/{page_id}.png (description
   # text over the deliverable's uploaded Thumbnail, or a black canvas); this
   # static URL is only used as a fallback when the origin can't be derived.
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
   Then click **Initialize** on a fresh test Project in Notion. In Frame's web UI, open the workspace's Active Projects view; the new project appears as a top-level entry. Click into it: discipline subfolders nest inside. Add a leveranse with Type=Eksteriør, click its button, expect `Eksteriør/<project>_..._<leveranse>_V00.png` (the placeholder sits directly under the discipline folder — no per-leveranse wrapping folder). Both Notion rows should have their `Frame.io` URL populated; the Leveranse URL opens directly to the placeholder file's view in Frame.

## Layout

```
Frame.io workspace (FRAME_WORKSPACE_ID)
├─ 1234 Heimdal Solsletta bygg D     ← top-level Frame Project (FrameProjectFolder)
│  └─ (project's root_folder_id, auto-created)
│     ├─ Eksteriør/                  ← discipline folder, lazy & shared
│     │  ├─ 1234_..._Fasade Nord_V00.png   ← placeholder, FrameLeveranseFolder.frame_placeholder_file_id
│     │  └─ 1234_..._Fasade Sør_V00.png
│     ├─ Interiør/...
│     ├─ Animasjon/...
│     └─ Annet/...
├─ <ProjectName2>
│  └─ ...
```

Each Notion project becomes its own Frame Project entity (with its own `project_id`, its own `root_folder_id`, and its own active/inactive flag visible in Frame's UI). Discipline folder names live in `FRAME_DISCIPLINE_FOLDER_NAMES` (`config.py`).

The placeholder file bytes are **rendered on the fly per deliverable**. `sync_frame_leveranse` hands Frame's `create_file_from_url` the URL `https://hub.<domain>/assets/placeholder/<deliverable_page_id>.png` (origin derived from `FRAME_PLACEHOLDER_URL`); Frame fetches it over the Cloudflare tunnel. That endpoint ([routes/assets.py](../../src/gb_automations/routes/assets.py)) reads the deliverable's Notion row live and renders (Pillow) a 1080×1080 (1:1) PNG: the **`Beskrivelse`** text — falling back to the row title when blank — drawn over the **`Thumbnail`** upload, or a solid black canvas when no thumbnail is set. The endpoint is public + unauthenticated (same exposure as the static `/assets/` mount) and always returns a valid PNG — on any Notion/fetch error it degrades to a plain black image so Frame never stores a broken V00. Frame fetches the URL *once, asynchronously, after* the create call, so later edits to the description/thumbnail in Notion only show up on the **next** provisioning of that deliverable, not retroactively on the already-fetched file.

Placeholder files sit DIRECTLY under the discipline folder — there is no per-leveranse wrapping folder. The placeholder filename embeds the leveranse name (`<project>_<studio>_<leveranse>_V00.png`), which both guarantees uniqueness within a shared discipline folder and provides the visible label in Frame's UI. A leveranse rename in Notion PATCHes the file's name in Frame to match (the file id is preserved, so the version stack and cached comment joins survive the rename).

The cache row stores BOTH the Frame Project id (`frame_project_id`) and its root folder id (`frame_folder_id`). The Project id is what we rename via `PATCH /projects/{id}` and what future active/inactive automation will toggle on; the root folder id is what discipline subfolders parent under. On `FrameLeveranseFolder`, `frame_folder_id` holds the (shared) discipline folder id and `frame_placeholder_file_id` is the per-leveranse anchor that comment-sync, version-sync, and the stale-check all key on.

## Operations

- **Status**: every Frame task lights the same Projects-DB status dot as the existing label/NAS flows — 🔄 active, ⚠️ retrying, 🛑 failed, ✅ idle. A Frame outage shows up as a yellow/red dot on the affected projects.
- **Queue visibility**: `GET /debug/queue` lists pending/in-progress/failed task counts and surfaces the last error per failed task; task types are `frame_project_sync` and `frame_task_sync`.
- **Retry a stuck task**: `docker compose exec api python -m gb_automations.scripts.retry_failed` flips every `failed` Frame task back to `pending`. Or click the Notion button again — idempotent.
- **Self-heal**: if you delete a Frame Project by hand, the next sync detects the stale id (a 404 on `get_project`), evicts the cache row, and recreates the Project from scratch. Per-leveranse self-heal keys on the placeholder file (a 404 on `get_file` evicts the row). Cross-discipline moves are not yet implemented (a discipline change logs a warning and renames the placeholder in place).
- **Adoption**: if a Frame Project with the matching name already exists in the workspace (e.g. someone created it by hand in Frame's UI before clicking Initialize), the sync ADOPTS it instead of creating a duplicate. Same adoption logic applies to discipline folders and to placeholder files (matched by filename).
- **Legacy wrapping folders**: leveranses provisioned before the flatten still have an orphaned per-leveranse folder sitting alongside the placeholder under the discipline folder. The cache row's `frame_folder_id` still points at the old wrapper (not the discipline folder), but no code path keys on that column anymore (the stale-check uses the placeholder file id; comment + version syncs use the placeholder file id). The orphaned wrappers are cosmetic debris — safe to delete by hand in Frame's UI.

## Re-running the bootstrap

Re-run only if the refresh token is invalidated (Adobe password change, credential rotation) or you need to change account/workspace. The script clears stale state in `/tmp` before starting; pasted values overwrite the previous ones in `.env`.

---

## Phase 2.5 — Frame ↔ Notion comment + status loop

What this adds on top of Phase 1:

- **Frame comments → Notion.** The first comment on a delivered version (V01+) lazily creates a `Korreksjonsrunde N` sub-row under the deliverable (in the Oppgaver DB). Each comment then becomes a `Korreksjon` row in the **Korreksjoner DB**, related to that round. Replies nest under the parent comment's Korreksjon (3-level, via Parent item). Each Korreksjon has a `Ferdig` checkbox.
- **Bidirectional `Ferdig` ↔ `completed_at` sync.** Ticking the Notion checkbox on a Korreksjon PATCHes the Frame comment's completed state; resolving the comment in Frame ticks the Notion checkbox. Loop-prevented via read-first/skip-if-same.
- **Auto-managed `Status` select on the deliverable row (Oppgaver DB).** Transitions:
  - V01+ uploaded in Frame → **Ferdig**.
  - First client comment on the new version → **Klar til oppstart**.
  - Any Korreksjon checkbox ticked → **Under arbeid**.
  - All Korreksjon checkboxes done → **Oppgaver ferdig**.
  - Next version uploaded → loops back to Ferdig.
- **Manual override.** Setting Status to `Trenger avklaring` or `Utgår` suppresses all auto-writes for that deliverable until the team manually moves it out.

### Operator setup (Phase 2.5)

1. **Add a `Status` select property to the Oppgaver DB** with these options (in order, Norwegian labels):
   - `Klar til oppstart`
   - `Trenger avklaring`
   - `Under arbeid`
   - `Oppgaver ferdig`
   - `Ferdig`
   - `Utgår`

2. **Add a `Ferdig` checkbox property to the Korreksjoner DB** (for the individual feedback items). The Oppgaver DB has NO Ferdig checkbox — a round's completion is signalled by the deliverable `Status` reaching `Oppgaver ferdig`, not a per-round checkbox.

3. **Enable sub-items on BOTH DBs.** Newer Notion auto-creates the `Parent item` relation. In the Oppgaver DB, Korreksjonsrunde rows are sub-items of their deliverable. In the Korreksjoner DB, reply rows are sub-items of the parent comment's row. The default relation label is `"Parent item"` (`OPPGAVER_PROPS["parent"]` / `KORREKSJONER_PROPS["parent"]` in `config.py`) — rename if your workspace uses a different label.

4. **Add the `Korreksjonsrunde` relation to the Korreksjoner DB** pointing at the Oppgaver DB. Default name `Korreksjonsrunde` (`KORREKSJONER_PROPS["korreksjonsrunde"]`). Each Korreksjon row relates to its Korreksjonsrunde N row (which lives in Oppgaver); the status rollup counts children via this relation.

5. **Set up two Notion automations on the Korreksjoner DB** (the lightning-bolt menu → New automation):
   - **Automation 1**: When `Ferdig` is checked → Send webhook → `https://hub.<your-domain>/webhooks/notion/oppgave-done`. Add a custom header `Authorization: Bearer <NOTION_WEBHOOK_SECRET>` (the same secret your other Notion buttons use).
   - **Automation 2**: When `Ferdig` is unchecked → same URL, same header.

   Two automations are required because Notion's checkbox triggers are direction-specific. The receiver doesn't care which one fired — it reads the row's current state at process time. (The endpoint path is still `/oppgave-done` for backward compatibility; it now guards on `KORREKSJONER_DB_ID`.)

6. **Re-register the Frame webhook** to subscribe to the `file.versioned` event (in addition to the comment events from Phase 2):
   ```
   docker compose exec api python -m gb_automations.scripts.frame_register_webhook
   ```
   The script detects the existing webhook's event list, sees `file.versioned` is missing, deletes + recreates with the wider set. It prints a fresh `FRAME_WEBHOOK_SECRET=...` — paste into `.env`.

7. **Restart the api**:
   ```
   docker compose up -d --force-recreate api
   ```

8. **Verify**:
   ```
   curl https://hub.<your-domain>/debug/frame/webhooks
   ```
   Should show the webhook subscribed to: `comment.created`, `comment.updated`, `comment.completed`, `comment.uncompleted`, `comment.deleted`, `file.versioned`.

### End-to-end test

1. **First delivery → Ferdig.** Drag a real image as V01 onto a deliverable's V00 placeholder in Frame's UI. Within a few seconds: the deliverable's `Status` flips to **Ferdig**. Logs: `[frame:...] file.versioned ... enqueued frame_version_sync`.

2. **Client comment → Klar til oppstart.** Post a comment on V01 in Frame. A `Korreksjonsrunde 1` sub-row appears under the deliverable in the Oppgaver DB, and a `Korreksjon` row appears in the Korreksjoner DB (related to that round, holding the comment text + a `Ferdig □` checkbox). The deliverable's `Status` flips to **Klar til oppstart**.

3. **Resolve comment in Frame → Under arbeid.** Click ✓ on the comment in Frame's UI. The Notion `Korreksjon` row's `Ferdig` checkbox auto-checks. Status → **Under arbeid**.

4. **Tick checkbox in Notion → Frame updates.** On another open Korreksjon row, check `Ferdig` in Notion. Within seconds, the linked Frame comment's `completed_at` populates (visible by reopening the comment in Frame's UI — it shows ✓ resolved).

5. **All done → Oppgaver ferdig.** Resolve the last open comment in either tool. The deliverable's `Status` → **Oppgaver ferdig** — that status IS the round-done signal (there's no per-round checkbox).

6. **Re-upload V02 → Ferdig.** Drag a new image as V02. Status → **Ferdig**. The old Korreksjonsrunde 1 stays in Notion as history.

7. **Manual override.** Manually flip Status to `Trenger avklaring`. Post another comment in Frame. Korreksjonsrunde 2 + the Korreksjon row get created, but Status stays at `Trenger avklaring` — auto-writes are suppressed.

8. **Internal task is ignored.** Create an Oppgaver row with `Type=Klargjøre modell` and click the Sync button. Nothing is provisioned in Frame or on the NAS (the webhook returns `skipped`, reason "Type is not a discipline"). Only rows whose `Type` is a real discipline (Interiør/Eksteriør/Animasjon/Annet) are deliverables.

### Loop-prevention model

The bidirectional sync between Frame's `completed_at` and Notion's `Ferdig` could loop forever:

- Notion checkbox → engine PATCHes Frame → Frame webhook fires → engine writes Notion → Notion automation fires → …

It doesn't, because both `notion_client.set_row_done` and `notion_client.set_deliverable_status` are **read-first / skip-if-same**:

- When we write to Notion to mirror Frame's state, the Notion automation fires back.
- The receiver enqueues `oppgave_done_sync`.
- The engine reads the Frame side and sees the comment is ALREADY in the same state we'd write — no PATCH back.

One round-trip per click, no ping-pong.

### What status transitions can fire

| Trigger | Engine | Target status |
|---|---|---|
| `file.versioned` webhook | `sync_frame_version` | `Ferdig` |
| First top-level `comment.created` of round N | `sync_frame_comments` | `Klar til oppstart` |
| Any Korreksjon `Ferdig` toggled (either side) | `sync_leveranse_status` (rollup) | `Under arbeid` or `Oppgaver ferdig` |

The rollup engine runs after every Korreksjon state change. It reads the active Korreksjonsrunde's children count and decides the target. Pure read-then-decide logic.

### Things to know

- **V00 comments are dropped.** The placeholder image isn't a real deliverable; comments on it are pre-delivery noise and get logged + skipped.
- **Pre-restructure rounds stay as historical debris.** Korreksjonsrunde/Korreksjon rows created before the Oppgaver+Korreksjoner restructure remain in the (renamed) Korreksjoner DB. They aren't migrated. The status rollup looks for the active round as a sub-row in the Oppgaver DB, so a deliverable mid-correction-cycle at cutover shows `skipped_no_round` until the next NEW comment creates a fresh round in Oppgaver and the rollup resumes — expected, not a bug.
- **Comments on Frame files we don't track are skipped.** The engine only acts on files whose id is cached as a `FrameLeveranseFolder.frame_placeholder_file_id` (or whose version stack contains such a file).
