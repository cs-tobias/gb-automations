# Frame.io V4 Shares API — findings (as of 2026-05-28)

The client asked for Frame.io review links to **auto-update**: when a new discipline folder is added
to a project, the share link should pick it up without anyone editing it by hand
(*"Automatisk oppdatere frame link så nye folders blir lagt til"*).

We built the full feature, then probed the live V4 API on the dev account. **The write side of the
Shares API does not exist yet**, so the feature is not buildable today. This note records exactly what
works, what doesn't, and the design — so we can finish it quickly when Frame ships the missing routes.

> TL;DR: **reading shares works; creating / modifying / attaching-assets does not.** Pickup-only
> wasn't worth shipping (it doesn't deliver the actual ask), so the code was discarded. Revisit when
> the changelog shows create-share + add-asset routes.

## What works (read side)

- `GET /v4/accounts/{aid}/projects/{project_id}/shares` → 200. Returns the project's shares.
- `GET /v4/accounts/{aid}/shares/{share_id}` → 200.
- A share object's reviewer-facing URL field is **`short_url`** (e.g. `https://f.io/okYIzP_i`).
  **Confirmed from real data** — this was one of the unknowns; it's pinned.
- A real share object's full key set (no `type`/discriminator field on the object itself):
  ```
  access, collection_id, commenting_enabled, created_at, description, downloading_enabled,
  enabled, expiration, id, last_viewed_at, name, passphrase, short_url, updated_at
  ```
- A share is backed by a **collection**: `GET /v4/accounts/{aid}/collections/{collection_id}` → 200,
  returns `{ project_id, aggregation_mode: "static", auto_generated: true, root_folder_id: null, … }`.

## What does NOT work (write side — the whole feature)

Tested live on the dev account 2026-05-28, account `12622664-…`:

| Call | Result |
|---|---|
| `POST /v4/accounts/{aid}/projects/{pid}/shares` (create) | **400 Bad Request** for every body shape tried |
| `POST /v4/accounts/{aid}/shares/{sid}/assets` (add folder) | **404 "no route found" (DeveloperApiWeb.V0.Router)** |
| `GET  /v4/accounts/{aid}/shares/{sid}/assets` (list assets) | **404 "no route found"** |
| `PATCH /v4/accounts/{aid}/shares/{sid}` (edit share) | **400 Bad Request** |
| `GET/POST /collections/{cid}/assets`, `/children`, `/items`, `/files` | **all 404 "no route found"** |
| `/shares/{sid}/children`, `/shares/{sid}/shared_assets`, `/shares/{sid}/items` | **all 404 "no route found"** |
| `GET /collections/{cid}?include=assets` | **422 "Unexpected field: assets"** (so no asset expansion) |

### Create-share body probing (all failed)

The create endpoint demands a discriminator but rejects every value we found:
- `{"data": {"name": "x"}}` → 422 *"Value used as discriminator for `type` matches no schemas"*.
- `{"data": {"name": "x", "type": "review|presentation|share|shares|…"}}` → 422 *"No value provided
  for required discriminator `type`"* (i.e. the value isn't a recognized schema key).
- `type` at the **top level** (sibling of `data`) → 422 *"Unexpected field: type"*.
- `name` at the top level → 422 *"Unexpected field: name"* (so fields DO belong inside `data`).
- `{"data": {"type": "public|private", …}}` → **400 Bad Request** (no field-level detail). This is the
  closest we got: `public`/`private` are accepted as discriminator *values* (the 422 disappears), but
  the request still 400s with no useful error body — consistent with the endpoint being gated /
  not-fully-shipped rather than a body-shape problem.

### Confirmation from Frame.io

- Dev-forum thread *"Forced V4 upgrade removes share link API automation…"* — Frame staff
  (RobertLoughlin, **2026-05-18**): *"POST /shares only accepts `downloading_enabled` and `name`… We
  are currently working on closing the API parity gap ahead of June 1st. This includes updating the
  Shares API… being able to list assets and change/set settings. We anticipate these being added very
  soon — keep an eye on the changelog."*
- The web UI **can** add folders to a share (confirmed by Tobias), so it's an API gap, not a plan
  limitation.

## Conclusion

The client's actual ask (auto-create a share + auto-attach each new discipline folder) requires the
create-share and `/shares/{id}/assets` routes, which are **not shipped** as of 2026-05-28. The only
thing buildable today is read-only pickup (team makes the link manually in Frame; we copy `short_url`
into Notion on the next Sync) — judged not worth building. **The share code was discarded; HEAD has no
share code.**

## Re-check before rebuilding (when Frame ships it)

1. Watch the changelog: <https://developer.adobe.com/frameio/guides/Changelog/> (and the dev forum).
2. Re-probe the live API. Pin three things:
   - **create-share body** — full `data` shape + the required `type`/discriminator value.
   - **add-asset body** — leading guess was `POST /shares/{id}/assets` with `{"data": {"id": <folder_id>}}`.
   - **folder-is-live** — attach a *folder*, upload a new file into it, confirm the file appears in the
     share (vs. a static snapshot). The whole "auto-grows" promise depends on this.
   - Share URL field is already confirmed: **`short_url`**.

## Preserved design (the discarded implementation)

The version built (and discarded) hooked entirely off the existing Sync-button path — no new webhook
or `sync_tasks.task_type`. Summary so it can be rebuilt fast:

- **Trigger:** in `sync/sync_frame.py` `sync_frame_leveranse`, immediately after the
  `_ensure_discipline_folder(...)` call — the single chokepoint where a discipline folder is
  created/found.
- **Client methods** (`clients/frame.py`, mirroring the existing token → `{"data": {...}}` →
  `_with_retries` → `_raise_for_status` → `_unwrap` pattern):
  `list_project_shares(project_id)`, `create_share(project_id, name)`, `get_share(share_id)`,
  `add_asset_to_share(share_id, asset_id)` (swallow the duplicate-attach status as a no-op).
- **Data model** (`models.py`): two nullable columns on `FrameProjectFolder`
  (`frame_share_id`, `frame_share_url`) + a new `FrameShareAttachment` table
  `(frame_share_id, frame_folder_id)` composite PK, plus `project_page_id` (indexed) + `discipline_key`.
  Row existence == "folder attached to share" — the DB is the idempotency authority, NOT the in-memory
  `_discipline_folder_cache` (so a folder that already existed in Frame still gets attached once).
- **Engine helpers** (`sync/sync_frame.py`):
  - `_share_view_url(share_obj, share_id)` — prefer `short_url`, fall back across candidates → `""`.
  - `_ensure_project_share(session, project_row, share_name)` — self-heal cached id via `get_share`
    (404 → clear id/url + drop that share's attachment rows), else adopt-by-name via
    `list_project_shares`, else `create_share`. Share name = project's stable `current_name`.
  - `_attach_folder_to_share(session, share_id, folder_id, project_page_id, discipline_key)` —
    SELECT the attachment row → present means no-op; else `add_asset_to_share` + INSERT
    (ON CONFLICT DO NOTHING).
- **Notion writeback:** new `PROJECTS_REVIEW_URL_PROP = "Delingslenke"` in `config.py` +
  `set_project_review_url(project_page_id, url)` in `clients/notion.py` (verbatim copy of
  `set_project_toggl_url`/`set_project_frame_url`). The Projects DB needs a `Delingslenke` URL property
  or the PATCH 400s (best-effort, so it just stays blank). Distinct from `Frame.io` (internal view URL).
- **Migration:** add the two columns + the table; off whatever the head revision is at rebuild time.
- All share work is best-effort / wrapped so a share failure never fails the folder + placeholder sync.

A throwaway probe route `GET /debug/frame/share-probe?project_id=…&folder_id=…` was used to discover
all of the above; it (and the rest of the share code) was removed in the discard.
