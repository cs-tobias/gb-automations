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

## Emails YYYY (Emails DB, year-partitioned)

Auto-created by code

---

## Projects DB

App is schema-agnostic — only the DB ID is needed (`PROJECTS_DB_ID`). Existing properties stay as-is.
