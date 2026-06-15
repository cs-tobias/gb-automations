from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Lets Field(validation_alias=...) read alternate env-var names while
        # the Python attribute keeps its canonical name. Used for the
        # Phase 2 TASKS_DB_ID → LEVERANSER_DB_ID rename so legacy .env
        # files keep working until they're migrated.
        populate_by_name=True,
    )

    env: str = "dev"
    database_url: str = "postgresql+asyncpg://gb:gb@db:5432/gb"

    # Google Workspace + service account (for Gmail via DWD impersonation)
    workspace_domain: str = ""
    google_service_account_json: str = ""

    # Mailboxes the backend syncs, comma-separated, e.g.
    #   SYNCED_MAILBOXES=petter@goldbox.no,anna@goldbox.no
    # This is the single source of truth for the `users` table: on startup the
    # app reconciles users to exactly match this list (listed → active, any
    # other previously-active user → inactive). Declarative so a fresh
    # `docker compose up` self-seeds — no manual seed_users step. Edit the list
    # in .env and `--force-recreate api` to add/remove a mailbox.
    synced_mailboxes: str = ""

    @property
    def synced_mailbox_list(self) -> list[str]:
        """Parsed, lowercased, de-duplicated mailbox emails (order preserved)."""
        seen: dict[str, None] = {}
        for raw in self.synced_mailboxes.split(","):
            email = raw.strip().lower()
            if email:
                seen.setdefault(email, None)
        return list(seen)

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
    # Notion "Oppgaver" DB — one row per deliverable entity (image / render)
    # OR a general internal task. The two are distinguished by the `Type`
    # select: a real discipline (Interiør/Eksteriør/Animasjon/Annet) means
    # "deliverable" and gets Frame provisioning; any other Type (e.g.
    # "Klargjøre modell") or a blank Type means "internal task" and is
    # skipped. Korreksjonsrunde sub-rows also live here (sub-items of their
    # deliverable).
    #
    # History of this env var: it was "Oppgaver"/`tasks_db_id`, briefly
    # renamed to "Leveranser"/`LEVERANSER_DB_ID`, now back to "Oppgaver"
    # with the collapsed structure. The DB id never changes on a Notion
    # rename — operators point this at the same DB throughout. Reads
    # OPPGAVER_DB_ID, falling back to LEVERANSER_DB_ID / TASKS_DB_ID for
    # .env files that haven't been migrated yet.
    #
    # When set, the Notion button webhook accepts clicks from pages in this
    # DB and (for deliverable rows) provisions NAS folders + a Frame folder
    # + V00 placeholder under the project's discipline.
    oppgaver_db_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "OPPGAVER_DB_ID", "LEVERANSER_DB_ID", "TASKS_DB_ID"
        ),
    )
    # Notion "Korreksjoner" DB — individual feedback items, one row per Frame
    # comment (replies nested via Parent item). Each row relates to its
    # Korreksjonsrunde row (which lives over in the Oppgaver DB). Auto-
    # populated by the Frame comments engine on `comment.created`. Empty
    # during rollout = features that need to write here log and skip.
    #
    # NOTE: requires its own KORREKSJONER_DB_ID env var — it deliberately
    # does NOT fall back to the old OPPGAVER_DB_ID name, because that name
    # now resolves to the deliverables DB above. At cutover, operators set
    # KORREKSJONER_DB_ID to the id the old OPPGAVER_DB_ID held.
    korreksjoner_db_id: str = Field(
        default="",
        validation_alias=AliasChoices("KORREKSJONER_DB_ID"),
    )
    # Optional: live mirror of the durable sync queue so the client can watch
    # what's queued / processing / failed in Notion. If empty, the mirror is a
    # no-op (the Postgres queue still works, observable via /debug/queue).
    sync_queue_db_id: str = ""
    # Optional: write an at-a-glance sync status dot (🟢/🔴/⚪) onto each Projects
    # DB page via the `Sync` Select property. Off by default; requires the
    # property + its emoji options to exist on the Projects DB. Independent of
    # sync_queue_db_id — you can have the dot without the detail DB, or both.
    projects_sync_status: bool = False

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
    # Overridden by docker-compose.yml to host.docker.internal (Ollama runs
    # natively on the host, not in a container). This default is for running
    # the app outside Docker (e.g. `uv run`), where localhost reaches Ollama.
    ollama_base_url: str = "http://localhost:11434"
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

    # Per-contact byte-exact signature-image learning (contact_signature_images
    # table). After the same image sha1 from the same sender appears in this
    # many DISTINCT Gmail threads, future emails from them skip those bytes.
    # Conservative default — a real recurring photo with distinct bytes per
    # send never accumulates. Raise (e.g. SIGNATURE_LEARN_THRESHOLD=5) only
    # if a sender's logo got wrongly learned before we add a UI to un-learn.
    signature_learn_threshold: int = 3

    # Project-provisioning fan-out toggles. One Notion button → one webhook
    # (/webhooks/notion) provisions a project across every relevant target;
    # each target is independently switchable so we can decouple while building.
    #   sync_gmail_labels — create/rename the Gmail label in every mailbox.
    #     Turn OFF while testing the NAS step so button clicks don't churn labels.
    #   sync_nas_folders — create/rename the project folder on the office NAS.
    #     OFF by default until the mount is configured on the office host.
    sync_gmail_labels: bool = True
    sync_nas_folders: bool = False

    # Office NAS (the shared `W:` drive). Docker mounts the share itself as
    # a CIFS volume (see docker-compose.yml `nas` volume + NAS_CIFS_DEVICE /
    # NAS_USER / NAS_PASS env vars) — this works uniformly on Linux hosts
    # and Windows Docker Desktop with the WSL2 backend, where binding a
    # Windows-mapped drive letter or UNC path does NOT work (gotchas §15).
    # nas_host_path: the Windows-facing display path written back into the
    #   Projects-DB "NAS" URL column so the team can click and open the
    #   folder in File Explorer (e.g. "W:\Prosjekt" or "W:\gb-automations-test").
    #   COSMETIC ONLY — it is NOT used to mount the share. Empty → no Notion
    #   writeback (the folder still gets created, the URL column stays blank).
    # nas_projects_root: mounted root inside the container, e.g.
    #   "/mnt/nas/Prosjekt". The NAS step is inert unless this is set AND
    #   sync_nas_folders is true.
    # nas_received_subfolder: subfolder created inside each project for
    #   incoming client/email files. Goldbox calls it "Mottatt" (Received).
    nas_host_path: str = ""
    nas_projects_root: str = ""
    nas_received_subfolder: str = "Mottatt"

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
    # Workspace-level UUID of the custom "Status" select field in Frame.io.
    # Resolved once via `GET /v4/accounts/{aid}/metadata/field_definitions`
    # (also surfaced at GET /debug/frame/field-definitions). Empty → the
    # Notion ↔ Frame Status reconcile engine is disabled (returns "skipped"
    # with a clear note rather than crashing).
    frame_status_field_id: str = ""
    # Phase 1 fan-out toggle (mirrors sync_gmail_labels / sync_nas_folders).
    # OFF by default so a deploy can land the Frame code without flipping the
    # behavior on. Flip to true once frame_workspace_id + frame_placeholder_url
    # are configured AND the OAuth bootstrap has run successfully.
    #
    # Each Notion project becomes its own top-level Frame Project under
    # settings.frame_workspace_id — there is no shared "Goldbox" parent
    # project, so no FRAME_ROOT_PROJECT_ID setting. (An earlier design did
    # use one; if you still have it in .env it's silently ignored.)
    sync_frame: bool = False
    # Publicly-fetchable URL Frame.io's create_file_from_url endpoint can GET
    # to seed the per-task placeholder asset. We host the bytes ourselves at
    # /assets/Goldbox_Logo_White.png (mounted by main.py); set this to the
    # public form, e.g. https://hub.<domain>/assets/Goldbox_Logo_White.png —
    # Frame fetches it over the existing Cloudflare tunnel, no S3 needed.
    # Swap the asset under /assets/ to change the image without redeploying.
    frame_placeholder_url: str = ""
    # The studio identifier baked into every Frame placeholder filename:
    #   <project>_<studio>_<task>_V00.jpg
    # Renaming the studio later only affects NEW placeholders — existing
    # uploads keep their old filename (Frame's version stack is keyed by file
    # slot, not name, so re-naming after the fact is purely cosmetic).
    frame_filename_studio: str = "Goldbox.no"

    @property
    def placeholder_render_base(self) -> str:
        """Public origin (scheme://host[:port]) the dynamic placeholder endpoint
        is reachable at, derived from `frame_placeholder_url`.

        The dynamic per-deliverable placeholder lives at
        `<origin>/assets/placeholder/<page_id>.jpg`. We derive the origin from
        the already-required `frame_placeholder_url` (the static fallback,
        e.g. https://hub.<domain>/assets/placeholder.png) rather than adding a
        second env var — they're always the same host (our own FastAPI app
        behind the Cloudflare tunnel). Empty when frame_placeholder_url is
        unset; callers fall back to the static URL.
        """
        url = self.frame_placeholder_url.strip()
        if not url:
            return ""
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme}://{parts.netloc}"

    # Toggl Track (timesheet integration).
    # Single workspace-admin API token. The Reports v3 API returns every
    # workspace member's entries when called by an admin, so one token is
    # enough — no per-user setup. Get the token from
    # https://track.toggl.com/profile after signing in as the admin.
    # toggl_workspace_id is resolved during the bootstrap script (printed
    # for .env paste). Phase 1 is polling-only; webhooks may be added later.
    toggl_api_token: str = ""
    toggl_workspace_id: str = ""
    # Phase 1 fan-out toggle (mirrors sync_gmail_labels / sync_nas_folders /
    # sync_frame). OFF by default so the code can land without flipping
    # behavior on. Flip to true once toggl_api_token + toggl_workspace_id
    # are populated AND the bootstrap script has run successfully.
    # When true, the Notion Initialize button also enqueues a
    # toggl_project_sync alongside the label / nas / frame fan-out.
    sync_toggl: bool = False

    # Phase 2 — daily hours aggregation. Independent of sync_toggl so the
    # project mirror can run without the hours sync. When true:
    #   - the APScheduler job `toggl_hours_daily` runs at 02:00 Europe/Oslo
    #     and enqueues a `toggl_hours_sync` task
    #   - the worker pulls the last 14 days from Toggl Reports v3, aggregates
    #     per (user, project, day) in Europe/Oslo time, and upserts rows
    #     into the year-partitioned `Timer YYYY` Notion DB
    # The Toggl→Notion user attribution is by email match: each Toggl user's
    # email must equal a Notion workspace user's email. No manual mapping.
    sync_toggl_hours: bool = False
    # Parent page under which yearly `Timer YYYY` databases live (same
    # pattern as emails_parent_page_id). The hours engine auto-creates
    # `Timer 2026`, `Timer 2027`, etc. on first use.
    toggl_timer_parent_page_id: str = ""
    # How many days back to re-pull on every nightly run. Toggl Reports v3
    # has no reliable updated_since for cross-user queries, so the engine
    # re-reads the window and overwrites the corresponding Notion rows —
    # retroactive timesheet edits within this window propagate. 32 days
    # covers the full calendar month so every nightly run rechecks the
    # entire current month — payroll totals are always up-to-date regardless
    # of when in the month the run happens.
    toggl_hours_window_days: int = 32
    # DEV-ONLY: rewrite Toggl user emails before the Notion match. Format is a
    # comma-separated list of `toggl_email=notion_email` pairs. Set when your
    # personal Toggl + Notion accounts don't share an email (production
    # Goldbox accounts do, so this is left blank there). Example:
    #   TOGGL_DEV_EMAIL_OVERRIDES=tobias@cinesuit.com=tobias@my-notion.com
    # An override only swaps the email used for the Notion-user lookup — the
    # Toggl side (TogglUserCache, time-entry attribution, user-name display)
    # is untouched.
    toggl_dev_email_overrides: str = ""

    @property
    def toggl_dev_email_overrides_map(self) -> dict[str, str]:
        """Parse TOGGL_DEV_EMAIL_OVERRIDES into a lowercased {toggl: notion} dict.

        Silently drops malformed entries (missing '=' or empty side); the
        engine logs the parsed result once at sync start so misconfiguration
        is visible without crashing the run.
        """
        out: dict[str, str] = {}
        for pair in self.toggl_dev_email_overrides.split(","):
            pair = pair.strip()
            if not pair or "=" not in pair:
                continue
            left, right = pair.split("=", 1)
            left = left.strip().lower()
            right = right.strip().lower()
            if left and right:
                out[left] = right
        return out

    # Fiken (Norwegian accounting). Two halves:
    #   1. CREATION — Notion buttons on a Project enqueue
    #      `fiken_invoice_create` tasks that POST a DRAFT invoice to Fiken
    #      (oppstart at the project's configured percentage, slutt = remainder).
    #      The user reviews + sends from Fiken's UI.
    #   2. POLLER — APScheduler enqueues a singleton `fiken_poll` task that
    #      lists sent invoices/offers and upserts them into year-partitioned
    #      `Fakturaer YYYY` / `Tilbud YYYY` Notion DBs (replaces the Make
    #      automation that matches on the project name in the reference field).
    # Personal API token (Bearer header). Created at *Rediger konto →
    # Sikkerhet → Personlige API-nøkler* in Fiken. Non-expiring, revocable.
    fiken_api_token: str = ""
    # Company slug from `GET /companies`. Every endpoint is scoped via
    # `/companies/{slug}/…`. Single-tenant Goldbox → one slug suffices.
    fiken_company_slug: str = ""
    # Parent Notion page under which yearly `Fakturaer YYYY` / `Tilbud YYYY`
    # databases auto-create (same pattern as emails_parent_page_id /
    # toggl_timer_parent_page_id).
    fiken_parent_page_id: str = ""
    # How often the poller runs. 30 min is fine — invoices are low-velocity
    # and we don't need real-time mirror.
    fiken_poll_interval_minutes: int = 30
    # On the first poll after enabling, how far back to backfill. 90 days
    # covers a normal accounting quarter; older invoices stay in Fiken.
    fiken_first_poll_window_days: int = 90
    # Feature flag — OFF by default. When true: the scheduled poller runs,
    # the Notion fiken-create-invoice webhook accepts requests, and
    # _validate_required_settings requires the token + slug + parent page id
    # + projects_db_id. Flip to true once the personal-account smoke test
    # passes.
    sync_fiken: bool = False


settings = Settings()


# Names of the properties on the Emails database. Norwegian to match the other
# Goldbox DBs (Kontaktpersoner, Kunder). Change here if you renamed them in
# Notion. Property types expected:
#   subject       Emne        (title)
#   thread_id     Thread ID   (rich_text)
#   message_id    Message ID  (rich_text)        ← dedup key
#   project       Prosjekt    (relation → Projects)
#   from_contact  Fra         (relation → Contacts, single)   ← the sender
#   to_contacts   Til         (relation → Contacts, multi)    ← To recipients
#   cc_contacts   Kopi        (relation → Contacts, multi)    ← Cc recipients
#   date          Dato        (date)
#   tags          Tagger      (multi_select)
#   body          Melding     (rich_text)        ← full cleaned message body, chunked
#   files         Vedlegg     (files)            ← attachments uploaded to Drive, linked here
EMAILS_PROPS = {
    "subject": "Emne",
    # Technical Gmail IDs — kept in English; never surfaced to the team in views.
    "thread_id": "Thread ID",
    "message_id": "Message ID",
    "project": "Prosjekt",
    "from_contact": "Fra",
    "to_contacts": "Til",
    "cc_contacts": "Kopi",
    "date": "Dato",
    "tags": "Tagger",
    "body": "Melding",
    "files": "Vedlegg",
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
    # Relation → Fakturamottaker DB. The Fakturamottaker row holds the
    # company's Orgnr (Norwegian organization number) which is the
    # authoritative key for resolving the matching Fiken customer.
    # Read-only for the Fiken creation engine.
    "fakturamottaker": "Fakturamottaker",
}


# The relation property on the Projects DB that links a project to its
# Kunder (Companies) row. Read-only for the Fiken creation engine; the
# engine walks Project → Kunder → Fakturamottaker → Orgnr to resolve the
# Fiken customer.
PROJECTS_KUNDER_PROP = "Kunder"


# Properties on the Fakturamottaker DB. Only the Orgnr is read by the
# Fiken creation engine — Fakturamottaker rows carry additional address
# / billing-recipient fields that this app doesn't touch.
FAKTURAMOTTAKER_PROPS = {
    "orgnr": "Orgnr",   # rich_text — Norwegian organization number
}


# Live mirror of the durable sync queue (sync_queue_db_id). One row per active
# task so the client can watch what's queued / processing / failed. Edit these
# to match the actual property names in the Notion DB the operator creates.
SYNC_QUEUE_PROPS = {
    "subject": "Emne",        # title
    "status": "Status",       # select: Queued / Processing / Failed
    "thread_id": "Thread ID",  # rich_text — dedup key for the mirror row
    "project": "Prosjekt",    # rich_text (project label/name; kept simple, not a relation)
    "attempts": "Forsøk",     # number
    "error": "Feil",          # rich_text
    "queued_at": "Lagt i kø", # date
}

# The Status select option labels, surfaced in the Notion view.
SYNC_QUEUE_STATUS_QUEUED = "Queued"
SYNC_QUEUE_STATUS_PROCESSING = "Processing"
SYNC_QUEUE_STATUS_FAILED = "Failed"


# At-a-glance sync status on the Projects DB. A single Select property whose
# option NAMES are icon-only — the chip shows just a status icon, no word, so it
# reads as a sync indicator rather than a project-lifecycle "Active/Idle". Notion
# Select can't be truly nameless, so the icon IS the name. Legend:
#   🔄 syncing now · ⚠️ failed once, retrying · 🛑 failed (needs attention) · ✅ synced/idle
# The property name lives here; edit it to match the column you add in Notion.
PROJECTS_SYNC_PROP = "Sync"
# Sibling rich-text property to PROJECTS_SYNC_PROP. The queue worker writes
# "<done>/<total>" here while a project has active/failed tasks, so the user
# can see live progress (e.g. "11/23") next to the icon during a project
# resync. Cleared when the project goes idle, same condition as the ✅ icon.
# Field name lives here; edit to match the column you add in Notion.
PROJECTS_SYNC_PROGRESS_PROP = "Sync progress"
PROJECT_SYNC_ACTIVE = "🔄"
PROJECT_SYNC_RETRYING = "⚠️"
PROJECT_SYNC_FAILED = "🛑"
PROJECT_SYNC_IDLE = "✅"

# Maps the queue's internal state strings (project_sync_state) to the Notion
# select option. Notion auto-creates an option the first time we write it, so
# these don't need pre-creating — but the names must match EXACTLY (emoji
# included) or you get duplicate chips. None → clear the property (we use an
# explicit Idle chip instead, so "no dot" never looks like "not set up").
PROJECT_SYNC_OPTION = {
    "active": PROJECT_SYNC_ACTIVE,
    "retrying": PROJECT_SYNC_RETRYING,
    "failed": PROJECT_SYNC_FAILED,
    "idle": PROJECT_SYNC_IDLE,
}


# URL properties each provisioner writes back to the Projects DB so a row is
# one click away from the corresponding system. All three columns share the
# same pattern: filled iff the project has been provisioned in that system,
# empty otherwise — so the row itself is the "has this been wired up?"
# indicator. Rename here if a column header changes in Notion.
#   "Gmail"    URL — opens the project's Gmail label in whichever Goldbox
#              mailbox the user is signed into.
#   "NAS"      URL (text-style; Notion accepts non-http schemes) — Windows path
#              to the project folder on the office share, e.g. W:\Prosjekt\...
#              Filled only when NAS_PROJECTS_DISPLAY_ROOT is configured.
#   "Frame.io" URL — Frame.io view URL for the project folder.
# Both Projects and Tasks DBs have a Frame.io column (one per row of each).
PROJECTS_GMAIL_URL_PROP = "Gmail"
PROJECTS_NAS_URL_PROP = "NAS"
PROJECTS_FRAME_URL_PROP = "Frame.io"
OPPGAVER_FRAME_URL_PROP = "Frame.io"
# Deliverable-row fields that feed the dynamically-rendered Frame placeholder
# (the V00 file). The render endpoint reads these live at fetch time:
#   "Beskrivelse" (rich_text) — text drawn over the placeholder; falls back to
#                 the row's title (Navn) when blank.
#   "Thumbnail"   (files)     — optional uploaded reference image used as the
#                 background; a plain black canvas is used when empty.
OPPGAVER_DESC_PROP = "Beskrivelse"
OPPGAVER_THUMB_PROP = "Thumbnail"
# Toggl Track project URL — written back to the Projects DB by
# sync_toggl_project so a single click in Notion opens the matching
# project's timer dropdown / Reports view in Toggl. Same column-as-
# indicator pattern as the others.
PROJECTS_TOGGL_URL_PROP = "Toggl"


# Top-level Gmail label namespace for project labels. The full path we create
# per project is "Prosjekt/<year>/<project-name>" — Gmail uses `/` as the
# hierarchy separator and auto-creates parent labels on demand. Norwegian
# "Prosjekt" (singular) to match the Goldbox Windows server's folder naming, so
# the Gmail labels, the Drive attachment folders (derived from this same path),
# and the on-prem server all share one namespace.
# Hardcoded (not env-configurable) on purpose: changing it after labels exist
# would orphan every nested label in every mailbox (the ProjectLabel table
# keys by Gmail label ID, so renames-by-ID still work, but the backfill script
# looks up labels by full name and would no longer find them) — so a change here
# means re-syncing to mint labels at the new prefix.
PROJECTS_LABEL_PREFIX = "Prosjekt"


# Project disciplines — which branches of Goldbox's NAS folder template get
# created for a project. Derived as the union of `Type` values across this
# project's tasks (Oppgaver DB), so a project has no separate discipline
# property to maintain — the tasks are the single source of truth.

# Notion select label (lowercased) → canonical discipline key.
DISCIPLINE_KEYS = {
    "interiør": "interior",
    "eksteriør": "exterior",
    "animasjon": "animation",
    "annet": "other",
}

# Canonical key → on-disk folder name. The template spells the animation folder
# differently by location ("Animation" under Arbeidsfiler/3ds max/scenes vs
# "Animasjon" under Media), so the mapping is split per location.
DISCIPLINE_FOLDER_SCENES = {
    "interior": "Interioer",
    "exterior": "Eksterioer",
    "animation": "Animation",
    "other": "Annet",
}
DISCIPLINE_FOLDER_MEDIA = {
    "interior": "Interioer",
    "exterior": "Eksterioer",
    "animation": "Animasjon",
    "other": "Annet",
}

# Canonical key → on-Frame.io folder name. Unlike the NAS template, Frame.io
# has no scenes-vs-media split — each project gets one discipline folder per
# active discipline, with placeholders sitting directly inside (no per-leveranse
# wrapping folder). Norwegian to match what Goldbox sees in Notion's `Type`
# select; the leaf names line up visually when a team member scans the NAS and
# Frame side by side.
FRAME_DISCIPLINE_FOLDER_NAMES = {
    "interior": "Interiør",
    "exterior": "Eksteriør",
    "animation": "Animasjon",
    "other": "Annet",
}


# Names of the properties on the Oppgaver database. Every row here is one of:
#   - a deliverable entity (image/render) — Type is a real discipline;
#   - a general internal/prep task — Type is "Klargjøre modell" (or any other
#     non-discipline value, or blank);
#   - a Korreksjonsrunde N sub-row under a deliverable — kind=Korreksjonsrunde.
# `Type` carries BOTH axes: the four disciplines (Interiør/Eksteriør/Animasjon/
# Annet) mean "deliverable, in this discipline"; anything else (e.g. "Klargjøre
# modell") means "internal task — no Frame/NAS provisioning". DISCIPLINE_KEYS
# normalizes the disciplines to canonical keys (interior/exterior/animation/
# other); a Type that isn't in DISCIPLINE_KEYS is the internal-task marker, so
# the Frame/NAS gate is simply "is Type a recognized discipline?". The
# Korreksjonsrunde sub-rows reuse the `kind` (Type) + `round` + `parent` slots.
OPPGAVER_PROPS = {
    "name": "Navn",            # title
    "project": "Prosjekt",     # relation → Projects DB
    "discipline": "Type",      # single_select: Interiør / Eksteriør / Animasjon / Annet / Klargjøre modell
    # Deliverable lifecycle state. Driven by Frame events + Korreksjon
    # rollup (see STATUS_* constants). Left blank on internal-task rows.
    # This select IS the round-done signal too: when all of a round's
    # Korreksjoner are done, the rollup sets the deliverable to
    # `Oppgaver ferdig` — there's no per-round Ferdig checkbox.
    "status": "Status",
    # Korreksjonsrunde sub-rows reuse these two. On deliverable / internal
    # rows they're unused. `kind` shares the `Type` column with discipline —
    # a Korreksjonsrunde sub-row carries Type="Korreksjonsrunde", which is
    # outside the discipline set, so the two never collide on one row.
    "kind": "Type",
    "round": "Runde",          # number — round N on Korreksjonsrunde sub-rows
    # Self-referential "sub-items" relation (Notion auto-creates it when
    # sub-items are enabled; default label "Parent item"). Korreksjonsrunde
    # sub-rows point at their deliverable.
    "parent": "Parent item",
    # Fiken billing columns (only meaningful on deliverable rows). `Pris`
    # drives the invoice line's unitAmount. The two `Skal …` checkboxes
    # are the "pick which rows to bill" UX (user ticks before clicking
    # the project-level Opprett-faktura button). `Fakturert` is the
    # multi-select carrying the row's billing state — engine-written
    # after a successful draft creation. See FAKTURERT_LABELS below for
    # the four option names + their state-machine semantics.
    "price_per_row": "Pris",                              # number (NOK)
    "should_invoice_at_start": "Skal oppstartsfaktureres",  # checkbox
    "should_invoice_at_end": "Skal sluttfaktureres",      # checkbox
    "billed_status": "Fakturert",                         # multi_select
}


# The four option names that may appear in the `Fakturert` multi-select
# on an Oppgaver row. Engine reads them to decide eligibility, writes
# them after billing.
#
# State machine:
#   (empty)                                — never billed; both Skal-* eligible
#   oppstart                               — 50% billed on a startup invoice; slutt eligible
#   oppstart + slutt                       — fully billed across two invoices (50/50)
#   slutt_full                             — fully billed on a single slutt invoice (no oppstart)
#   kreditert                              — a credit note has voided this row; engine never re-bills
#                                            (Phase B reads only; Phase C will write this when the
#                                            poller sees a credit note in Fiken)
#
# Engine NEVER writes `kreditert` — operator (or Phase C) sets it
# manually when a draft is cancelled or credit-noted in Fiken.
FAKTURERT_LABEL_OPPSTART = "Oppstartsfakturert"
FAKTURERT_LABEL_SLUTT = "Sluttfakturert"
FAKTURERT_LABEL_SLUTT_FULL = "Sluttfakturert (full)"
FAKTURERT_LABEL_KREDITERT = "Kreditert"

FAKTURERT_LABELS_ALL = (
    FAKTURERT_LABEL_OPPSTART,
    FAKTURERT_LABEL_SLUTT,
    FAKTURERT_LABEL_SLUTT_FULL,
    FAKTURERT_LABEL_KREDITERT,
)

# "Any slutt-side label is present" — used by the eligibility filter to
# decide a row has already been finally-billed (either split or single).
FAKTURERT_LABELS_SLUTT_DONE = (
    FAKTURERT_LABEL_SLUTT,
    FAKTURERT_LABEL_SLUTT_FULL,
)


# Properties added to the Projects DB for the Fiken integration.
# `last_draft_url` is engine-written after each successful draft creation
# so the user can jump straight to the draft in Fiken's UI.
#
# Oppstart/slutt split is hardcoded — oppstart always bills 50%, slutt
# bills the remainder (100% on its own / 50% if oppstart ran first).
# Simpler operator UX than a per-project percentage; one click does the
# right thing.
PROJECTS_FIKEN_PROPS = {
    # One URL per invoice side. Each Fiken draft is recorded under its
    # own column so clicking "slutt" doesn't overwrite the link to the
    # earlier "oppstart" draft. Engine writes the side that just ran;
    # the other side stays untouched.
    "oppstart_draft_url": "Oppstartsfaktura",  # url
    "slutt_draft_url": "Sluttfaktura",         # url
}


# Names of the properties on the Korreksjoner database — individual feedback
# items, one row per Frame comment (replies nested via `parent`). Each row
# relates to its Korreksjonsrunde row, which lives over in the Oppgaver DB.
KORREKSJONER_PROPS = {
    "name": "Navn",                        # title — the clean comment text
    # The Frame commenter, as a relation to the Contacts DB (find-or-created by
    # email — Frame requires a name + email to comment, so there's always one).
    # The contact carries the name + email, so we don't duplicate those onto the
    # row. Configured as a two-way relation so each Contact shows its comments.
    "commenter": "Kommentert av",          # relation → Contacts DB
    # date — when the comment was made in Frame (comment.created_at).
    "commented_at": "Dato",
    # relation → Oppgaver DB: the Korreksjonsrunde N row this comment belongs to.
    "korreksjonsrunde": "Korreksjonsrunde",
    # relation → Projects DB: the project this comment's deliverable belongs to.
    # Denormalized onto every Korreksjon (incl. replies) so the feedback list is
    # filterable/groupable by project at a glance, without traversing the
    # Korreksjonsrunde → deliverable → Prosjekt chain.
    "project": "Prosjekt",
    # No `kind` discriminator: every row in this DB is a Korreksjon by
    # construction, so storing the type on each row was redundant.
    "round": "Runde",                      # number — inherited from the round
    # Checkbox. Bidirectionally syncs with the Frame comment's `completed`
    # state: a Notion toggle PATCHes Frame; a Frame ✓ writes here.
    "done": "Ferdig",
    # Self-referential "sub-items" relation: a reply Korreksjon points at the
    # parent comment's Korreksjon row (3-level nesting in Notion).
    "parent": "Parent item",
}

# `Type` value that marks an Oppgaver-DB sub-row as a correction-round
# container. A Korreksjonsrunde is a sub-row under a deliverable, auto-created
# on the first Frame comment of round N (round=N), and carries Type=
# "Korreksjonsrunde" — outside the discipline set, so the discipline gate
# ignores it. (The Korreksjoner DB no longer stores a per-row kind: every row
# there is a Korreksjon by construction.)
KORREKSJON_KIND_KORREKSJONSRUNDE = "Korreksjonsrunde"


# Phase 2.5 — Status options on the Oppgaver DB's `Status` select (deliverables).
# Names match exactly what's configured in Notion's select-option list.
# State machine:
#   Ferdig (V01+ uploaded)
#     → Klar til oppstart (client comments arrived → Korreksjonsrunde N created)
#     → Under arbeid (team ticked at least one Korreksjon checkbox)
#     → Oppgaver ferdig (all this round's Korreksjon checkboxes done)
#     → Ferdig (team uploaded V0(N+1) — file.versioned event)
# Manual-only (automation respects, never overwrites):
#   Trenger avklaring, Utgår.
STATUS_FERDIG = "Ferdig"
STATUS_KLAR_TIL_OPPSTART = "Klar til oppstart"
STATUS_UNDER_ARBEID = "Under arbeid"
STATUS_OPPGAVER_FERDIG = "Oppgaver ferdig"
STATUS_TRENGER_AVKLARING = "Trenger avklaring"
STATUS_UTGAAR = "Utgår"

# When a deliverable's status is one of these, every auto-write is
# suppressed. Comments still arrive, Korreksjonsrunder + Korreksjoner still
# get created, but Status stays where the team manually put it. The team has
# to move it out manually before the automation can write again.
MANUAL_DELIVERABLE_STATUSES = frozenset({STATUS_TRENGER_AVKLARING, STATUS_UTGAAR})


# Projects DB lifecycle Status select — separate from the Oppgaver Status
# above (Oppgaver Status is per-deliverable; this one is per-project). Drives
# auto-provisioning when a project's status changes in Notion: Tilbudsfase →
# Gmail labels, Tilbud godkjent → NAS, I produksjon → Toggl + Frame. Cumulative
# (a later status also fires every earlier-status engine — all four engines are
# idempotent, so re-running them on an already-provisioned project is a no-op).
# Names must match the option labels in Notion exactly.
PROJECTS_STATUS_PROP = "Status"
PROJECT_STATUS_TILBUDSFASE = "Tilbudsfase"
PROJECT_STATUS_TILBUD_GODKJENT = "Tilbud godkjent"
PROJECT_STATUS_KLAR_TIL_OPPSTART = "Klar til oppstart"
PROJECT_STATUS_VENTER_AVKLARING = "Venter på avklaring"
PROJECT_STATUS_I_PRODUKSJON = "I produksjon"
PROJECT_STATUS_LANG_PAUSE = "Lang pause"
PROJECT_STATUS_FERDIG = "Ferdig"
PROJECT_STATUS_TAPT = "Tapt"

# Status option name → which provisioning engines auto-fire. Cumulative by
# design: I produksjon includes everything from the earlier stages so a project
# that skips a stage still gets its earlier syncs (idempotent — re-runs are
# no-ops for already-provisioned systems). Engines not in a stage's set are
# left untouched. Statuses not in this map (Klar til oppstart / Venter på
# avklaring / Lang pause / Ferdig / Tapt) are no-ops — recognized but explicitly
# do nothing, so the team can later wire them up (e.g. an archive flow on
# Ferdig) without changing the dispatch shape.
PROJECT_STATUS_AUTO_PROVISION: dict[str, set[str]] = {
    PROJECT_STATUS_TILBUDSFASE: {"gmail"},
    PROJECT_STATUS_TILBUD_GODKJENT: {"gmail", "nas"},
    PROJECT_STATUS_I_PRODUKSJON: {"gmail", "nas", "frame", "toggl"},
}

# Status options that deactivate the project's Frame.io entity. When a
# project transitions INTO one of these, the project-status webhook also
# enqueues a `frame_project_status_sync` task that PATCHes the Frame
# project to `status="inactive"` (V4 endpoint: PATCH
# /v4/accounts/{aid}/projects/{pid}). Every OTHER status (including
# unmapped ones like `Klar til oppstart` / `Venter på avklaring` /
# `Lang pause`) flips it back to `active` — so reopening a finished
# project automatically un-inactivates its Frame entity.
#
# Notion-only: the reverse direction (Frame archived → Notion Status)
# is NOT mirrored. Notion is the source of truth for project lifecycle
# (see CLAUDE.md). Skipped silently when the project has no Frame entity
# provisioned yet (no FrameProjectFolder row).
PROJECT_STATUS_INACTIVE_TRIGGERS: frozenset[str] = frozenset({
    PROJECT_STATUS_FERDIG,
    PROJECT_STATUS_TAPT,
})

# Titles that mark a row as "not yet a real project" — auto-triggered
# provisioning (the /notion/project-status webhook) skips while the row still
# carries one of these. Goldbox creates new projects by duplicating a template
# row literally named "000_Kunde_Prosjekt TEMPLATE"; auto-provisioning the
# template name would mint garbage labels/folders, and two new placeholder
# rows existing at the same time would collide on a single shared Gmail label
# (label create-by-name is idempotent in Gmail, so both ProjectLabel rows
# would write the same label_id — see sync_labels._create_label_for_all_users).
# The companion Name-edited automation on the Projects DB fires
# /notion/sync-gmail once the team renames the row, so users don't have to
# re-touch Status to kick off provisioning after a rename.
# Manual buttons (/notion/sync-gmail and friends) intentionally do NOT consult
# this set — the team can still force-sync a placeholder name on purpose.
# Matched exact, case-sensitive, post-strip. Add entries here if the template
# row's name changes in Notion.
PROJECTS_PLACEHOLDER_TITLES: frozenset[str] = frozenset({
    "000_Kunde_Prosjekt TEMPLATE",
})

# The literal string we write to Frame.io's custom "Status" select field
# when reflecting a Notion deliverable that's at Utgår. Matches the Notion
# option name verbatim — the Frame workspace must have an option with the
# same spelling on its Status field for the bidirectional mirror to work.
FRAME_STATUS_UTGAAR = "Utgår"


# Timer YYYY DB — auto-created per year, year-partitioned the same way
# Emails YYYY is. One row per (Ansatt, Prosjekt, Dato) tuple per year.
# That tuple is also the upsert key the engine uses, encoded into the two
# rich_text id columns at the end (Toggl IDs are the SOURCE-OF-TRUTH
# identifier — relations/people can be reassigned in Notion, ids can't).
TIMER_PROPS = {
    # The title slot. Composed as "{Navn} — {Dato}" so the row is
    # human-readable in any view. Notion requires a title; we'd rather
    # have something meaningful than a blank.
    "name": "Navn",
    "date": "Dato",                  # date — the local Europe/Oslo calendar day
    # `people` property pointing at a native Notion user. The engine looks
    # up the Notion user UUID by matching the Toggl user's email against
    # Notion's workspace members. Lets each employee filter "Show my
    # hours" with Notion's built-in `current user` filter on this column.
    "employee": "Ansatt",
    "project": "Prosjekt",           # relation → Projects (empty when no Notion match)
    "hours": "Timer",                # number — decimal hours (e.g. 7.5)
    # Count of raw Toggl sessions aggregated into this row. Lets the
    # operator (and verify route) sanity-check that no entries were lost
    # in aggregation — sum across a window should equal Toggl's raw entry
    # count for that window. Independent metric from total hours.
    "entry_count": "Antall økter",
    # Aggregated unique entry descriptions for this (user, project, day) cell.
    # Joined with "; " and truncated to ~1900 chars so the row stays under
    # Notion's 2000-char rich_text limit.
    "description": "Beskrivelse",
    # Toggl's raw project name. Filled even when the relation is empty (so
    # unmatched entries are still attributable). Set to "Uten prosjekt" when
    # the Toggl entry had no project at all.
    "toggl_project_name": "Toggl Prosjekt navn",
    # Toggl's raw user name. Always populated so historical hours from
    # ex-employees (no longer in the Notion workspace) are still
    # attributable. The `Ansatt` people-property is set only when the
    # Toggl email matches an active Notion workspace member.
    "toggl_user_name": "Toggl Bruker navn",
    # Hidden technical columns — used by the engine to upsert / delete
    # without scanning the people property (which a user could edit). The
    # two ids together are the dedup key.
    "toggl_user_id": "Toggl User ID",
    "toggl_project_id": "Toggl Project ID",
}


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


def build_timer_db_schema(
    *,
    projects_db_id: str,
) -> dict[str, dict]:
    """Build the Notion property schema for a `Timer YYYY` database.

    The year router (`clients/notion_timer_db.py`) calls this when auto-
    creating a new year's DB. Property NAMES come from TIMER_PROPS (single
    source of truth — manual renames in Notion still propagate by editing
    that dict). Property TYPES are hard-coded here because they're part of
    the contract with the hours engine.

    Required:
      - `projects_db_id`: `Prosjekt` is a relation; we need the target DB ID.

    Note: `Ansatt` is a `people` property (Notion native users), so no extra
    db id is needed — Notion infers the workspace member set.
    """
    if not projects_db_id:
        raise ValueError(
            "projects_db_id is required to build the Timer DB schema "
            "(Prosjekt is a relation property)"
        )
    # Insertion order = column order in Notion. Title first (Notion
    # requirement), then the human-readable columns left-to-right (Dato,
    # Ansatt, Prosjekt, Timer, Beskrivelse, Toggl Prosjekt navn), then the
    # two hidden id columns at the end where they stay out of the way.
    return {
        TIMER_PROPS["name"]: {"title": {}},
        TIMER_PROPS["date"]: {"date": {}},
        TIMER_PROPS["employee"]: {"people": {}},
        TIMER_PROPS["project"]: {
            "relation": {
                "database_id": projects_db_id,
                "single_property": {},
            },
        },
        TIMER_PROPS["hours"]: {"number": {"format": "number"}},
        TIMER_PROPS["entry_count"]: {"number": {"format": "number"}},
        TIMER_PROPS["description"]: {"rich_text": {}},
        TIMER_PROPS["toggl_project_name"]: {"rich_text": {}},
        TIMER_PROPS["toggl_user_name"]: {"rich_text": {}},
        TIMER_PROPS["toggl_user_id"]: {"rich_text": {}},
        TIMER_PROPS["toggl_project_id"]: {"rich_text": {}},
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
