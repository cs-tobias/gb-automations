# Notion DB property names
Source of truth: [src/gb_automations/config.py](../src/gb_automations/config.py).
Any other property on this DB is untouched by the app.

---

## Kontaktpersoner (Contacts DB)

Navn (title)
E-post (email)
Telefon (phone)
Tittel (rich_text)
Adresse (rich_text)
Kunder (relation → Kunder)

---

## Kunder (Companies DB)

Navn (title)
Nettside (rich_text)
Kontaktpersoner (relation → Kontaktpersoner).

---

## E-post YYYY (Emails DB, year-partitioned)

Auto-created by code (title `E-post 2026`, one DB per year).

Emne (title)
Melding (rich_text)
Fra (relation → Kontaktpersoner)
Til (relation → Kontaktpersoner)
Kopi (relation → Kontaktpersoner)
Dato (date)
Vedlegg (files)
Tagger (multi_select)
Prosjekt (relation → Projects)
Thread ID (rich_text)
Message ID (rich_text)

---

## Projects DB

App is schema-agnostic — only the DB ID is needed (`PROJECTS_DB_ID`). Existing properties stay as-is.

---

## Oppgaver DB (deliverables + internal tasks + Korreksjonsrunde sub-rows)

`OPPGAVER_DB_ID` (falls back to `LEVERANSER_DB_ID` / `TASKS_DB_ID` for un-migrated .env files). One DB holding everything the team works on:

- **Deliverables** — `Type` is a real discipline (Interiør/Eksteriør/Animasjon/Annet). The Frame.io folder + version stack lives against this row. Only these get Frame/NAS provisioning.
- **Internal tasks** — `Type=Klargjøre modell` (or any other non-discipline value, or blank). General prep/project work. No Frame.
- **Korreksjonsrunde N** — sub-rows of a deliverable (via `Parent item`), `Type=Korreksjonsrunde`, auto-created on the first Frame comment of round N.

`Type` carries both axes: the four disciplines mean "deliverable, in this discipline" (and a view grouped by `Type` puts all Eksteriør work together); `Klargjøre modell` (or anything not in the discipline list) means "internal task". The Frame/NAS gate is simply "is `Type` a recognized discipline?". There is NO separate Kategori property.

Navn (title)
Prosjekt (relation → Projects)
Type (single_select) — Interiør / Eksteriør / Animasjon / Annet / Klargjøre modell (and `Korreksjonsrunde` on round sub-rows)
Frame.io (url) — auto-written by sync_frame_leveranse on deliverables
Beskrivelse (rich_text) — text drawn on the Frame placeholder image; falls back to the row title (Navn) when blank. Read live by the placeholder render endpoint.
Thumbnail (files) — optional uploaded reference image used as the placeholder background; a plain black canvas is used when empty.
Status (single_select) — deliverable lifecycle, see options below; auto-managed in Phase 2.5. Reaching `Oppgaver ferdig` IS the round-done signal — there is no per-round Ferdig checkbox.
Runde (number) — round N on Korreksjonsrunde sub-rows (engine plumbing for dedup + active-round detection; team can hide it in views)
Parent item (self-referential relation, Notion sub-items feature) — Korreksjonsrunde rows point at their deliverable

Status select options (Phase 2.5):

- `Klar til oppstart` — auto-set when first comment of a new round arrives
- `Trenger avklaring` — manual only (suppresses auto-writes)
- `Under arbeid` — auto-set when any Korreksjon `Ferdig` is checked
- `Oppgaver ferdig` — auto-set when all Korreksjon `Ferdig` boxes of the active round are checked
- `Ferdig` — auto-set when a new file version is uploaded in Frame (`file.versioned`)
- `Utgår` — manual only (suppresses auto-writes)

---

## Korreksjoner DB (individual feedback items, one row per Frame comment)

`KORREKSJONER_DB_ID` — must be set explicitly (does NOT fall back to the old `OPPGAVER_DB_ID` name, which now points at the deliverables DB above).

Navn (title) — author + comment text
Korreksjonsrunde (relation → Oppgaver, single page) — the Korreksjonsrunde N row this comment belongs to
Prosjekt (relation → Projects, single page) — the project this comment's deliverable belongs to; auto-written on every Korreksjon (incl. replies) so the feedback list is filterable/groupable by project
Runde (number) — inherited from the round; UX-only filter, not read by the engine
Ferdig (checkbox) — bidirectional Phase 2.5: ticking propagates to the linked Frame comment's `completed_at` and back
Parent item (self-referential relation, Notion sub-items feature) — a reply Korreksjon points at the parent comment's Korreksjon row (3-level nesting). Replies do NOT carry the Korreksjonsrunde relation, so they're excluded from the round's rollup count.

(No `Type` property — every row in this DB is a Korreksjon by construction.)
