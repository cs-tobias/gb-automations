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

## Leveranser DB (one row per deliverable image / render)

`LEVERANSER_DB_ID` — historically called the Oppgaver DB, renamed in Phase 2. Each row is a deliverable entity (e.g. "Stue v1"); the Frame.io folder + version stack lives against this row.

Navn (title)
Prosjekt (relation → Projects)
Type (single_select) — Interiør / Eksteriør / Animasjon
Frame.io (url) — auto-written by sync_frame_leveranse
Status (single_select) — see options below; auto-managed in Phase 2.5
Oppgaver (relation → Oppgaver) — inverse of Oppgaver.Leveranse

Status select options (Phase 2.5):

- `Klar til oppstart` — auto-set when first comment of a new round arrives
- `Trenger avklaring` — manual only (suppresses auto-writes)
- `Under arbeid` — auto-set when any Korreksjon `Ferdig` is checked
- `Oppgaver ferdig` — auto-set when all Korreksjon `Ferdig` boxes of the active round are checked
- `Ferdig` — auto-set when a new file version is uploaded in Frame (`file.versioned`)
- `Utgår` — manual only (suppresses auto-writes)

---

## Oppgaver DB (the actual tasks)

`OPPGAVER_DB_ID` — new in Phase 2. Three row kinds (see `Type` select).

Navn (title)
Leveranse (relation → Leveranser, single page)
Type (single_select) — see kinds below
Runde (number) — round N for Korreksjonsrunde/Korreksjon rows; null for Oppstart
Ferdig (checkbox) — bidirectional Phase 2.5: ticking propagates to the linked Frame comment's `completed_at` and back
Parent item (self-referential relation, auto-created by Notion's sub-items feature) — Korreksjon rows point at their Korreksjonsrunde; reply Korreksjon rows point at the parent comment's Korreksjon row (3-level nesting)

Type select options:

- `Oppstart` — auto-created on Leveranse Initialize. Pre-delivery work. Round empty.
- `Korreksjonsrunde` — auto-created on the first Frame comment of round N. Parent of N Korreksjon rows.
- `Korreksjon` — auto-created on every Frame comment. One row per comment. Replies are also Korreksjon rows nested under their parent (3-deep).
