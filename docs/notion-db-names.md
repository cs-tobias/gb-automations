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

App reads the DB by id (`PROJECTS_DB_ID`); existing properties stay as-is. The Fiken integration adds:

Faktura status (status) — drives the "Send faktura" button. Notion property type is **Status**, not Select. The engine reads either shape (fallback in `read_select_name`) but writes the Status wrapper. Options:

- `Til oppstartsfaktura` — next click bills 50% of every eligible Oppgave
- `Til avslutningsfaktura` (or its synonym `Til fakturering`) — next click bills the remainder of every eligible Oppgave
- `Oppstart fakturert` — terminal post-state after a successful oppstart run
- `Fakturert` — terminal post-state after a successful slutt run

Faktura merkes (rich_text) — Deres referanse on the Fiken invoice (`yourReference`). Optional; omitted from the draft when blank.

Fakturamottaker (relation → Fakturamottaker DB) — direct billing-recipient link, read by the Fiken engine (one hop: Project → Fakturamottaker → Orgnr). Per-project, so a project can bill a different entity than the parent Kunder uses by default. The Fiken engine also WRITES to the Fakturamottaker DB now (via `FAKTURAMOTTAKER_DB_ID`): when Brreg resolves a customer from a Kunder name (the Brreg-by-name fallback) the engine creates a new Fakturamottaker row with the Brreg name + Orgnr and links it to the Project; when Orgnr is already present, the engine PATCHes the existing row's title to Brreg's official `navn` if it differs. Kunder relation is still read by the Brreg-by-name fallback (the operator can leave Fakturamottaker blank and just link a Kunder; the engine searches Brreg and self-resolves).

Fakturert sum (rollup or formula, operator-managed) — sum of `Fakturert beløp` across the project's Oppgaver. Read-only display; the engine never writes here.

Faktura utkast (url) — engine-written link to the most recent draft in Fiken's UI. Single column (replaces the earlier two-column Oppstartsfaktura/Sluttfaktura split). Transient by design: the URL targets a Fiken DRAFT specifically; once the operator clicks Send in Fiken the draft becomes a real invoice on a different URL and this one 404s. That's the correct "draft graduated" signal — overwriting on the next run is fine, only the most recent draft is reachable anyway.

---

## Oppgaver DB (deliverables + internal tasks + Korreksjonsrunde sub-rows)

`OPPGAVER_DB_ID` (falls back to `LEVERANSER_DB_ID` / `TASKS_DB_ID` for un-migrated .env files). One DB holding everything the team works on:

- **Deliverables** — `Type` is a real discipline (Interiør/Eksteriør/Animasjon/Annet). The Frame.io folder + version stack lives against this row. Only these get Frame/NAS provisioning.
- **Internal tasks** — `Type=Klargjøre modell` (or any other non-discipline value, or blank). General prep/project work. No Frame.
- **Korreksjonsrunde N** — sub-rows of a deliverable (via `Parent item`), `Type=Korreksjonsrunde`, auto-created on the first Frame comment of round N.

`Type` carries both axes: the four disciplines mean "deliverable, in this discipline" (and a view grouped by `Type` puts all Eksteriør work together); `Klargjøre modell` (or anything not in the discipline list) means "internal task". The Frame/NAS gate is simply "is `Type` a recognized discipline?". A separate `Oppgave kategori` multi-select drives the Fiken line text and income account (see the Fiken section below) — those two are orthogonal: `Type` decides Frame/NAS provisioning; `Oppgave kategori` decides how the invoice line is labeled and booked.

Navn (title)
Prosjekt (relation → Projects)
Type (single_select) — Interiør / Eksteriør / Animasjon / Annet / Klargjøre modell (and `Korreksjonsrunde` on round sub-rows)
Frame.io (url) — auto-written by sync_frame_leveranse on deliverables
Beskrivelse (rich_text) — text drawn on the Frame placeholder image; falls back to the row title (Navn) when blank. Read live by the placeholder render endpoint. ALSO read by the Fiken engine: it goes into the line's `comment` sub-line on the printed invoice as "Navn - Beskrivelse" (with Kategori as the bold main line above).
Thumbnail (files) — optional uploaded reference image used as the placeholder background; a plain black canvas is used when empty.
Status (single_select) — deliverable lifecycle, see options below; auto-managed in Phase 2.5. Reaching `Oppgaver ferdig` IS the round-done signal — there is no per-round Ferdig checkbox.
Runde (number) — round N on Korreksjonsrunde sub-rows (engine plumbing for dedup + active-round detection; team can hide it in views)
Parent item (self-referential relation, Notion sub-items feature) — Korreksjonsrunde rows point at their deliverable

Fiken billing columns (only meaningful on deliverable rows — internal tasks + Korreksjonsrunde rows are skipped by the Fiken engine):

Pris (number, NOK) — current agreed full price for this deliverable. MUTABLE: the operator can renegotiate between oppstart and slutt, and the engine reads the live value each run. The engine sends Pris as `unitPrice` on every Fiken line, every run — `Antall` (Fiken's quantity) is what changes between modes: 0.5 on oppstart, `remaining/gross` on slutt.
Rabatt (number, Percent format) — per-row discount. Column is modeled as Notion's `Percent` number format so the operator sees the `%` suffix in Notion's UI and types `15` for 15%; Notion's API returns it as the fraction `0.15`, which the engine consumes directly. The engine forwards the rabatt to Fiken on the invoice line itself (`discount` field, as a percent), so the printed invoice shows "Pris kr X / Rabatt Y% / Sum kr Z" instead of a pre-rabattert unit price. Notion's `Fakturert beløp` and the audit trail record the post-rabatt NOK amount that was actually billed. Blank = no discount.
Fakturert beløp (number, NOK) — running total of NOK actually billed across all runs. Engine-written; a project-level rollup of this column drives the displayed "Fakturert sum" on the Project.
Oppgave kategori (multi_select) — Fiken product / service category. The FIRST selected label is matched (by exact name) against the operator-managed Fiken product catalogue; on match, the engine sends `productId` on the invoice line and Fiken auto-fills both the printed product name and the `incomeAccount` (3000 / 3020 / etc.) from the product itself. The mapping is cached per-(company_slug, kategori_label) in `fiken_product_by_kategori` after the first lookup. When the Kategori has no matching Fiken product, the engine falls back to a free-text line — Fiken rejects that with `incomeAccount is required for free-text lines` so the operator's prompt is "add the missing product in Fiken's UI and re-click."
Fakturert status (status) — picks what the engine does with this row on the next click. Notion property type is **Status**, not Select. Same reader fallback + Status writer as `Faktura status` on Projects. Options:

- `Ikke fakturert` (default) — never billed; oppstart + slutt both eligible
- `Fakturert 50%` — engine-written after a successful oppstart run; only slutt eligible
- `Fakturert` — engine-written after a successful slutt run (or operator-set to force-skip); engine never re-bills
- `Utgår` — operator-only; engine skips in every mode

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
