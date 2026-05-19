from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://gb:gb@db:5432/gb"

    # Google Workspace + service account (for Gmail via DWD impersonation)
    workspace_domain: str = ""
    google_service_account_json: str = ""

    # Notion integration
    notion_token: str = ""
    notion_api_version: str = "2022-06-28"

    # Notion: parent page under which yearly Emails DBs live. The integration
    # auto-creates `Emails YYYY` databases on demand (first email of each year),
    # so this page replaces the old single `EMAILS_DB_ID`. At Goldbox volume
    # (~7k emails/year) a single Notion DB starts to slow down past year 3;
    # year partitioning keeps every DB fresh while preserving thread continuity
    # via the `Thread ID` text property that spans years.
    emails_parent_page_id: str = ""
    contacts_db_id: str = ""
    # Companies graph. Goldbox manages it like Contacts — created manually in
    # Notion, ID discovered by setup_workspace. One row per email domain; the
    # display Name comes from the sender's signature when available, falling
    # back to a capitalized domain stem.
    companies_db_id: str = ""
    # Optional: if set, the Notion button webhook rejects clicks from pages whose
    # parent isn't this database. If empty, the parent check is skipped (the button
    # is only placed on the Projects DB template anyway).
    projects_db_id: str = ""

    # Webhook auth secrets
    notion_webhook_secret: str = ""

    # Cloudflare Tunnel — only consumed by docker-compose's cloudflared service,
    # but tracked here so .env validation is centralized.
    cloudflare_tunnel_token: str = ""

    # Gmail Pub/Sub push (Stage 4c).
    # PUBSUB_TOPIC: full topic name e.g. projects/PROJECT_ID/topics/gmail-events
    # PUBSUB_AUDIENCE: audience claim Pub/Sub signs JWTs with — defaults to the
    #   push endpoint URL (leave matching the value in the GCP subscription).
    # PUBSUB_SERVICE_ACCOUNT_EMAIL: the SA that signs the JWTs (the one chosen
    #   when creating the push subscription). Used to validate the iss/email claim.
    pubsub_topic: str = ""
    # Required when pubsub_topic is set. Must match the audience configured on
    # the GCP push subscription — typically the public endpoint URL, e.g.
    # https://hub.{your-domain}/webhooks/gmail. Empty default + startup check
    # in main.py so a missing value fails loudly at boot instead of silently
    # rejecting every Gmail push at runtime.
    pubsub_audience: str = ""
    pubsub_service_account_email: str = ""

    # Local LLM (Ollama). Used only for tagging today — see clients/llm.py.
    # Splitting/extraction is now regex-based (utils/history_extraction.py)
    # so we don't need the heavyweight long-output budgets the LLM splitter had.
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.1:8b-instruct-q4_K_M"
    # Tagging is ~10 output tokens, normally < 5s. We pad to 5 minutes because
    # the host machine can be slow (a dev laptop or a shared box) — under that
    # condition we'd rather wait than abort. Anything past 5 min is almost
    # certainly a stuck Ollama, not slow inference, and *should* fail.
    ollama_timeout_s: float = 300.0

    # History-split fallback runs only when the regex splitter under-splits an
    # exotic forward (rare). Output is larger (one JSON object per message in
    # the chain) and the prompt is bigger, so the call legitimately needs
    # longer than tagging. Same 5-minute reasoning as ollama_timeout_s — wait
    # out a slow box, but bail on a genuinely stuck one. Sync_thread already
    # runs the fallback in the background sync, so a slower one-shot call
    # doesn't block webhook ack.
    ollama_split_timeout_s: float = 300.0

    # Tagging via LLM classify(). ON by default — adds ~1-2s per Notion row
    # (one classify call against Ollama). Set TAGGING_ENABLED=false in .env to
    # disable. See EMAIL_TAGS below for the taxonomy.
    tagging_enabled: bool = True

    # Path to the markdown file containing the tagging-LLM system prompt.
    # Relative paths resolve from the project root (works on Windows and Mac
    # — see _load_classify_prompt in clients/llm.py). Each deployment can
    # ship a customized prompt (e.g. prompts/goldbox.md) without code edits.
    tagging_prompt_path: str = "prompts/default.md"

    # Google Drive folder name used for storing email attachments. Created on
    # first upload, one folder per user mailbox. Files inside get
    # "anyone with link can view" permission so Notion-rendered links work.
    attachments_folder_name: str = "Notion Email Attachments"

    # Frame.io V4 (OAuth Web App via Adobe IMS).
    # Goldbox is non-Enterprise, so Adobe's clean S2S credential isn't
    # available — we use OAuth Web App with offline_access scope and store a
    # long-lived refresh token. The whole team already shares one Frame.io
    # account (petter@goldbox.no), so a single refresh token represents the
    # studio. Bootstrap once via `python -m gb_automations.scripts.frame_oauth_bootstrap`.
    frame_client_id: str = ""
    frame_client_secret: str = ""
    # Stored after the one-time OAuth bootstrap. Adobe IMS refresh tokens
    # don't have an expiry under the offline_access scope, but they DO get
    # invalidated if the user changes their Adobe password or the API
    # credential is rotated — in which case re-run the bootstrap.
    frame_refresh_token: str = ""
    # The redirect URI registered in Adobe Developer Console for this app.
    # MUST match exactly (Adobe rejects mismatches at /authorize and /token).
    # Lives under the public hub domain because Adobe requires HTTPS even
    # for localhost — the FastAPI route /oauth/frame/callback handles it.
    frame_redirect_uri: str = ""
    # Account + workspace the integration writes to. Resolved once during
    # bootstrap (the script lists what the authenticated user can access and
    # prompts for the right one) so the runtime client never has to guess.
    frame_account_id: str = ""
    frame_workspace_id: str = ""
    # HMAC-SHA256 signing secret returned by Frame.io when the webhook is
    # created via POST /v4/.../webhooks. Empty until the webhook is created;
    # bootstrap script handles creation and stashes the secret here.
    frame_webhook_secret: str = ""


settings = Settings()


# Names of the properties on the Emails database. Change here if you renamed them
# in Notion. Property types expected:
#   subject       (title)
#   thread_id     (rich_text)
#   message_id    (rich_text)        ← dedup key
#   project       (relation → Projects)
#   from_contact  (relation → Contacts, single)   ← the sender
#   to_contacts   (relation → Contacts, multi)    ← To recipients
#   cc_contacts   (relation → Contacts, multi)    ← Cc recipients
#   date          (date)
#   tags          (multi_select)
#   body          (rich_text)        ← full cleaned message body, chunked
#   files         (files)            ← attachments uploaded to Drive, linked here
EMAILS_PROPS = {
    "subject": "Subject",
    "thread_id": "Thread ID",
    "message_id": "Message ID",
    "project": "Project",
    "from_contact": "From",
    "to_contacts": "To",
    "cc_contacts": "Cc",
    "date": "Date",
    "tags": "Tags",
    "body": "Body",
    "files": "Files",
}

# Property names match Goldbox's existing Kontaktpersoner DB. Change the
# strings here if the DB is renamed in Notion; code references everywhere
# go through this dict.
CONTACTS_PROPS = {
    "name": "Navn",
    "email": "E-post",
    "phone": "Telefon",
    # Pulled from the sender's signature (title line below the name).
    "title": "Tittel",
    # Joined street + postal/city line from the signature.
    "address": "Adresse",
    # Single-relation to the Companies DB.
    "company": "Kunder",
    # Managed manually by Goldbox — mirrors project status, which lives
    # outside our automations. Code never writes this property.
    "company_status": "Kundestatus",
}


# Companies DB. Dedup key is the email domain ("Nettside" in Norwegian),
# stored as rich_text so Goldbox can freely rename the title — "Olavthon" →
# "Thon Eiendom" — without breaking the upsert path. Contacts relate IN to
# Company via CONTACTS_PROPS["company"]; the inverse relation on this DB
# ("Kontaktpersoner") is auto-maintained by Notion.
#
# Note: Goldbox sees "Nettside" but the value is the email domain
# (motionindex.io) rather than a full URL (https://motionindex.com). That's
# what we can reliably derive from every email; team is aware.
COMPANIES_PROPS = {
    "name": "Navn",
    "domain": "Nettside",
    "contacts": "Kontaktpersoner",
}


# Top-level Gmail label namespace for project labels. The full path we create
# per project is "Projects/<year>/<project-name>" — Gmail uses `/` as the
# hierarchy separator and auto-creates parent labels on demand.
# Hardcoded (not env-configurable) on purpose: changing it after labels exist
# would orphan every nested label in every mailbox (the ProjectLabel table
# keys by Gmail label ID, so renames-by-ID still work, but the backfill script
# looks up labels by full name and would no longer find them).
PROJECTS_LABEL_PREFIX = "Projects"


# Notion's multi-select API supports exactly these 10 colors. "default" is the
# greyish unstyled chip we want to avoid — leaving it out makes every tag chip
# carry a real color.
_NOTION_OPTION_COLORS = (
    "gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red",
)


def _color_for_tag(name: str) -> str:
    """Deterministic color pick for a tag name.

    Same name → same color forever, across re-creates and across year DBs.
    Helps the eye scan: `kjøkken` is always one color, `tilbud` always another.
    """
    # md5 is overkill but stable across Python versions (unlike builtin hash()
    # which is randomized per interpreter). We only need a few bytes.
    import hashlib

    digest = hashlib.md5(name.encode("utf-8")).digest()
    return _NOTION_OPTION_COLORS[digest[0] % len(_NOTION_OPTION_COLORS)]


def build_emails_db_schema(
    *,
    projects_db_id: str,
    contacts_db_id: str,
    tag_options: list[str],
) -> dict[str, dict]:
    """Build the Notion property schema for an `Emails YYYY` database.

    The year router calls this when auto-creating a new year's DB. Property
    NAMES come from EMAILS_PROPS (single source of truth — manual renames in
    Notion still propagate by editing that dict). Property TYPES are hard-
    coded here because they're part of the contract with the sync engine.

    Required:
      - `projects_db_id`: `Project` is a relation; we need the target DB ID.
      - `contacts_db_id`: `From`/`To`/`Cc` are all relations to Contacts; same.
      - `tag_options`: seeds the multi-select Tags property with EMAIL_TAGS so
        the schema is self-documenting (Notion auto-adds new tags as we write
        them).
    """
    if not projects_db_id:
        raise ValueError(
            "projects_db_id is required to build the Emails DB schema "
            "(Project is a relation property)"
        )
    if not contacts_db_id:
        raise ValueError(
            "contacts_db_id is required to build the Emails DB schema "
            "(From/To/Cc are relation properties)"
        )
    # Property insertion order is preserved by both Python dicts and Notion's
    # database-create API, so the order here IS the column order users see
    # left-to-right in Notion. Subject is pinned first because Notion requires
    # the title property to come first. Rest follows the agreed-on shape:
    # body & participants up front for at-a-glance scanning, then date/files,
    # then the categorical fields (Tags, Project), and the two technical ID
    # columns at the very end where they stay out of the way.
    return {
        EMAILS_PROPS["subject"]: {"title": {}},
        EMAILS_PROPS["body"]: {"rich_text": {}},
        EMAILS_PROPS["from_contact"]: {
            "relation": {
                "database_id": contacts_db_id,
                "single_property": {},
            },
        },
        EMAILS_PROPS["to_contacts"]: {
            "relation": {
                "database_id": contacts_db_id,
                "single_property": {},
            },
        },
        EMAILS_PROPS["cc_contacts"]: {
            "relation": {
                "database_id": contacts_db_id,
                "single_property": {},
            },
        },
        EMAILS_PROPS["date"]: {"date": {}},
        EMAILS_PROPS["files"]: {"files": {}},
        EMAILS_PROPS["tags"]: {
            "multi_select": {
                "options": [
                    {"name": t, "color": _color_for_tag(t)} for t in tag_options
                ],
            },
        },
        EMAILS_PROPS["project"]: {
            "relation": {
                "database_id": projects_db_id,
                "single_property": {},
            },
        },
        EMAILS_PROPS["thread_id"]: {"rich_text": {}},
        EMAILS_PROPS["message_id"]: {"rich_text": {}},
    }

# Multi-select tag taxonomy applied to each synced email by the local LLM.
# Two axes mixed in one flat list (one Notion `Tags` multi-select property):
#   1. Communication-type — what KIND of email is this? (workflow stage)
#   2. Topic/aspect       — what is the email ABOUT? (render subject matter)
# The LLM picks 1–3 tags total, typically one from each axis when both apply.
# Notion's multi-select auto-creates new option entries when we write them, so
# editing this list is the only step needed to add/remove tags.
EMAIL_TAGS = [
    # Communication-type (workflow / intent)
    "tilbud",         # offer / quote
    "bestilling",     # confirmed order
    "korreksjon",     # correction round
    "leveranse",      # delivery / final files
    "spørsmål",       # question / inquiry
    "underlag",       # briefing material / specs
    "møte",           # meeting
    "faktura",        # invoice
    "intern",         # internal Goldbox communication
    # Topic / aspect (architecture-render subject matter)
    "kjøkken",
    "bad",
    "stue",
    "soverom",
    "inngangsparti",
    "fasade",
    "korridor",
    "balkong",
    "utomhus",
    "plantegning",
    "detalj",
    "farger",
    # Fallback (LLM uses this only when nothing else fits)
    "annet",
]
