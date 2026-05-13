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


settings = Settings()


# Names of the properties on the Emails database. Change here if you renamed them
# in Notion. Property types expected:
#   subject    (title)
#   thread_id  (rich_text)
#   message_id (rich_text)        ← dedup key
#   project    (relation → Projects)
#   contacts   (relation → Contacts DB)
#   from_name  (rich_text)
#   from_email (email)
#   direction  (select: Incoming | Outgoing)
#   date       (date)
#   tags       (multi_select)
#   preview    (rich_text)
#   attachments (rich_text)       ← "had N attachments: [name1, name2]" text for now
EMAILS_PROPS = {
    "subject": "Subject",
    "thread_id": "Thread ID",
    "message_id": "Message ID",
    "project": "Project",
    "contacts": "Contacts",
    "from_name": "From",
    "from_email": "From Email",
    "direction": "Direction",
    "date": "Date",
    "tags": "Tags",
    "preview": "Preview",
    "attachments": "Attachments",
}

CONTACTS_PROPS = {
    "name": "Name",
    "email": "Email",
    "phone": "Phone",
    "company": "Company",
}
