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

Faktura status (status) — drives the "Send faktura" button AND records the terminal billed state. Notion property type is **Status**. The engine reads either Select or Status shape via `read_select_name`; writes go through the Status wrapper. Options:

- `Ikke fakturert` (default) — nothing planned
- `Til oppstartsfaktura` — operator: next click bills 50% of every eligible Oppgave
- `Til avslutningsfaktura` (or its synonym `Til fakturering`) — operator: next click bills the remainder
- `Fakturert 50%` — engine-written after the oppstart invoice is SENT (Phase C.3 graduation). Stays at the operator-picked value while a draft is in flight; the Faktura utkast URL + Postgres `FikenInvoice.sent_at IS NULL` row are the draft-in-flight signal.
- `Fakturert` — engine-written after the slutt invoice is SENT (or single-shot full bill)

History note: an earlier C.1 split this into two columns (history + handling). Reverted June 2026 — the `Faktura utkast` URL + the `Fakturaer` relation already make "draft in flight" / "we've sent X" visible without another status option. See CLAUDE.md → Fiken section for the rationale.

Faktura merkes (rich_text) — Deres referanse on the Fiken invoice (`yourReference`). Optional; omitted from the draft when blank.

Fakturamottaker (relation → Fakturamottaker DB) — direct billing-recipient link, read by the Fiken engine (one hop: Project → Fakturamottaker → Orgnr). Per-project, so a project can bill a different entity than the parent Kunder uses by default. The Fiken engine also WRITES to the Fakturamottaker DB now (via `FAKTURAMOTTAKER_DB_ID`): when Brreg resolves a customer from a Kunder name (the Brreg-by-name fallback) the engine creates a new Fakturamottaker row with the Brreg name + Orgnr and links it to the Project; when Orgnr is already present, the engine PATCHes the existing row's title to Brreg's official `navn` if it differs. Kunder relation is still read by the Brreg-by-name fallback (the operator can leave Fakturamottaker blank and just link a Kunder; the engine searches Brreg and self-resolves).

Fakturert sum (rollup or formula, operator-managed) — sum of `Fakturert beløp` across the project's Oppgaver. Read-only display; the engine never writes here.

Faktura utkast (url) — engine-written link to the most recent draft in Fiken's UI. Single column (replaces the earlier two-column Oppstartsfaktura/Sluttfaktura split). Transient by design: the URL targets a Fiken DRAFT specifically; once the operator clicks Send in Fiken the draft becomes a real invoice on a different URL and this one 404s. That's the correct "draft graduated" signal — overwriting on the next run is fine, only the most recent draft is reachable anyway.

Fakturaer (relation → Faktura DB, multi — Phase C.2) — reverse relation of the Faktura DB's `Prosjekt` property. Notion auto-syncs both sides; the engine writes the Faktura DB side (via the C.3 poller), Notion fills in the Projects side automatically. Operator never edits this directly. Lets you click a project and see every sent invoice + credit note linked to it.

---

## Faktura DB (sent invoices + credit notes — Phase C.2)

`FAKTURA_DB_ID`. Operator-managed DB that records every sent invoice + credit note the company has produced. Today Make.com populates it; Phase C.3's poller will replace Make. The Phase-C.2 writer ([sync/notion_faktura_db.py](../src/gb_automations/sync/notion_faktura_db.py)) creates rows here; it never re-PATCHes existing ones (sent invoices are immutable in practice — corrections happen via credit notes, which create new rows). Engine is dormant until the env var is set + Make is disabled.

Idempotency: every successful write inserts into `faktura_notion_cache(company_slug, record_type, fiken_record_id)` in Postgres. A repeat poll over the same Fiken record is a no-op (returns the cached page id). Operators are free to delete a Faktura row in Notion intentionally — the cache silently leaves it alone on subsequent polls (Notion is truth for "does this row exist or not").

Navn (title) — `invoiceNumber` (or `creditNoteNumber`) as a printable string. Used in lists / sorting by title text.
Fakturanummer (number) — same number as a plain integer. Sortable numerically; usable in Notion formulas.
Kreditnota til faktura (number) — credit notes only: the parent invoice's number (from `associatedInvoiceNumber`, fallback `associatedInvoiceId`). Plain integer, not a relation (operator-chosen shape, matches Make's existing rows). Blank on normal invoices.
Netto (number) — Fiken's `net` (before VAT), converted from øre to NOK (divide by 100).
Type (select) — `Faktura` or `Kreditnota`. Discriminates the two record types sharing this DB.
Kommentar (rich_text) — Fiken's `invoiceText` (invoices) or `comment` (credit notes). What the customer reads on the printed Kommentar field.
Fakturamottaker (relation → Fakturamottaker DB) — resolved by walking `customer.contactId → /contacts/{id} → organizationNumber → FAKTURAMOTTAKER_PROPS["orgnr"]` lookup. Left blank when no match.
Prosjekt (relation → Projects) — matched by `reference.our == project_title`. Left blank when no match. The reverse relation on Projects is `Fakturaer` (see Projects DB section).
Deres ref (rich_text) — `reference.yours` (yourReference). The reference the customer sent us / wants on their copy.
Dato (date) — Fiken's `issueDate` (YYYY-MM-DD). When the invoice was sent / finalized.
Vår ref (rich_text) — `reference.our` (ourReference = project title in our send_faktura flow). Denormalized text so it's visible without hopping the Prosjekt relation.
Fakturamottaker tekst (rich_text) — denormalized `customer.name`. Visible in the list view without clicking through.
URL (url) — Fiken UI page link for this record (works for both invoices + kreditnotas). Format: `https://fiken.no/foretak/{slug}/handel/salg/{saleId}` where `saleId` lives on the `sale.saleId` block of Fiken's payload. Pinned empirically 2026-06-21 — the earlier `/faktura/{invoiceId}` and `/kreditnota/{creditNoteId}` paths return blank pages.
Faktura PDF (url) — direct browser-clickable PDF download from Fiken's file store. Maps from `invoicePdf.downloadUrlWithFikenNormalUserCredentials` (invoices) or `creditNotePdf.downloadUrlWithFikenNormalUserCredentials` (kreditnotas). Auto-authenticates via the operator's existing Fiken session — clicking opens the printed PDF instantly. Falls back to the Bearer-only `downloadUrl` sibling if the browser-friendly one is missing.

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
Fakturert status (status) — single billing state column. Notion property type is **Status**. The engine reads either Select or Status shape; writes go through the Status wrapper. `send_faktura` does NOT write here at draft creation; the engine writes the terminal `Fakturert*` values only AFTER the draft has been graduated to a sent invoice (Phase C.3). Until then, rows stay at their previous value.

- `Ikke fakturert` (default) — nothing sent; both oppstart + slutt eligible
- `Fakturert 50%` — engine-written after the oppstart invoice is SENT; only slutt eligible
- `Fakturert` — engine-written after slutt SENT (or single-shot full bill); engine never re-bills
- `Utgår` — operator-only; engine skips in every mode

Eligibility additionally checks Postgres: a row whose page id appears in any `FikenInvoiceLine` joined to a `FikenInvoice` with `sent_at IS NULL` is on an unsent draft and skipped. Normally the per-project block check in `send_faktura` prevents this case from arising — there's at most one unsent draft per project — but the eligibility filter is defensive.

History note: an earlier C.1 added a separate `Faktura handling` column for intent + draft-in-flight state. Reverted June 2026 — the draft URL on the project + the Postgres audit trail convey the same information without adding a column to the Oppgaver UI.

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
