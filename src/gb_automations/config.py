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

    # Notion database IDs (find via /debug/databases after the integration is shared)
    emails_db_id: str = ""
    contacts_db_id: str = ""
    # Optional: if set, the Notion webhook only acts on pages parented to this database.
    # If empty, every page.created event the integration sees is treated as a project.
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
    pubsub_audience: str = "https://hub.tobiaseek.com/webhooks/gmail"
    pubsub_service_account_email: str = ""

    # Local LLM (Ollama). Used only for tagging today — see clients/llm.py.
    # Splitting/extraction is now regex-based (utils/history_extraction.py)
    # so we don't need the heavyweight long-output budgets the LLM splitter had.
    ollama_base_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.1:8b-instruct-q4_K_M"
    # Tagging is ~10 output tokens — short. 30s covers cold model load.
    ollama_timeout_s: float = 30.0

    # Tagging via LLM classify(). ON by default — adds ~1-2s per Notion row
    # (one classify call against Ollama). Set TAGGING_ENABLED=false in .env to
    # disable. See EMAIL_TAGS below for the taxonomy.
    tagging_enabled: bool = True

    # Google Drive folder name used for storing email attachments. Created on
    # first upload, one folder per user mailbox. Files inside get
    # "anyone with link can view" permission so Notion-rendered links work.
    attachments_folder_name: str = "Notion Email Attachments"


settings = Settings()


# Names of the properties on the Emails database. Change here if you renamed them
# in Notion. Property types expected:
#   subject    (title)
#   thread_id  (rich_text)
#   message_id (rich_text)        ← dedup key
#   project    (relation → Projects)
#   from_name  (rich_text)
#   from_email (email)
#   direction  (select: Incoming | Outgoing)
#   date       (date)
#   tags       (multi_select)
#   body       (rich_text)        ← full cleaned message body, chunked
#   files      (files)            ← attachments uploaded to Drive, linked here
EMAILS_PROPS = {
    "subject": "Subject",
    "thread_id": "Thread ID",
    "message_id": "Message ID",
    "project": "Project",
    "from_name": "From",
    "from_email": "From Email",
    "direction": "Direction",
    "date": "Date",
    "tags": "Tags",
    "body": "Body",
    "files": "Files",
}

CONTACTS_PROPS = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "company": "Company",
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
