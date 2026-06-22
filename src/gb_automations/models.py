from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from gb_automations.db import Base


class SyncCursor(Base):
    """Per-source position marker for incremental syncs.

    Will hold things like Gmail historyId per user (Stage 4 Pub/Sub) and
    Notion last_edited_time. Generic key/value design so we don't need a
    new table every time we add a source.
    """

    __tablename__ = "sync_cursors"

    source: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_value: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class OAuthToken(Base):
    """Live OAuth refresh token per provider, rotated in place.

    Adobe IMS rotates refresh tokens on every /token call — the response
    body carries a fresh refresh_token that replaces the one used to make
    the call, and the original token issued at bootstrap is valid for 14
    days. Each rotation resets that window, so as long as we capture the
    rotated value, the integration runs indefinitely.

    Storing the live token in Postgres (rather than relying on .env) means
    container rebuilds don't lose rotations, and the operator no longer has
    to re-paste tokens on every Adobe credential change.
    """

    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    refresh_token: Mapped[str] = mapped_column(String(2048), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base):
    """Workspace mailbox the backend should sync."""

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailRow(Base):
    """Local cache of "Gmail message → Notion row" mappings.

    Notion is the source of truth (we still query it on cache miss), but this
    table avoids hitting Notion's API on every dedup check. One row per
    Gmail message; message IDs are globally unique even when the same email
    lands in multiple inboxes.
    """

    __tablename__ = "email_rows"

    gmail_message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # Which user's mailbox surfaced this message to us (informational; for debugging
    # multi-recipient cases). The notion_page_id is shared across all users.
    seen_by_email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class EmailContentDedup(Base):
    """(project, from_email, sent_at_minute, body_hash) → notion_page_id of the canonical row.

    Third dedup layer for the case where Gmail splits one conversation into
    several threads (`KG9: …`, `Svar: KG9: …`, `Svar: Svar: KG9: …`). Each
    thread arrives independently; `sync_thread` correctly extracts historical
    rows from quoted bodies. Without this table the same logical email body
    would land in Notion multiple times — once for the real Gmail message and
    once per later thread that quoted it.

    The first two layers (EmailRow PK on gmail_message_id and Notion-query on
    `Message ID`) catch the same physical message arriving in multiple
    mailboxes. They DON'T catch identical content under different message_ids
    (real Gmail message vs synthetic id of its extracted-from-quote replica).
    This table closes that gap, scoped per project so generic short bodies
    ("Takk!" / "OK") only collapse within the same project.

    `sent_at_minute` is part of the key so two genuinely-distinct short
    replies ("Takk!") from the same sender don't collapse — they'll have
    different timestamps. The same logical email re-quoted across N threads
    carries the same minute-truncated timestamp and DOES collapse. Truncating
    to the minute absorbs sub-second drift between Gmail's `internalDate` and
    the regex-parsed "On X wrote:" date in quoted history.

    `body_hash` is SHA-256 hex of the *normalized* body (lowercased, whitespace
    collapsed, `[image: …]` markers stripped). Normalization absorbs the
    trivial drift that breaks an exact-byte hash when the same logical email
    is re-quoted across threads with slightly different surrounding framing.

    `from_email` is lower-cased and may be empty for LLM-extracted name-only
    senders (an empty-empty match across two rows still legitimately means
    "same body, same project, same timestamp, same nameless sender").
    """

    __tablename__ = "email_content_dedup"

    project_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_email: Mapped[str] = mapped_column(String(254), primary_key=True)
    sent_at_minute: Mapped[str] = mapped_column(String(16), primary_key=True)
    body_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The Gmail message id (real or synthetic) of the row that won the dedup.
    # Informational — for debugging which message a duplicate collapsed to.
    gmail_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContactCache(Base):
    """email → Notion contact page ID. Avoids Notion lookups on every sync.

    Email is the natural key — a person can have multiple addresses but we treat
    each address as its own contact (matches Apps Script behavior).
    """

    __tablename__ = "contact_cache"

    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class CompanyCache(Base):
    """email domain → Notion company page ID.

    Same shape as ContactCache but keyed by domain. The display name in Notion
    (e.g. "Thon Eiendom") can be edited freely by Goldbox without affecting
    dedup, because we look up by domain.
    """

    __tablename__ = "company_cache"

    domain: Mapped[str] = mapped_column(String(254), primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class AttachmentBlob(Base):
    """Drive link for a (content_sha1, drive_folder_path) pair — cross-thread durable.

    Replaces the older `thread_attachments` (thread-scoped) and the never-wired
    `attachment_fingerprints` (statistical sender-repeat) tables. Both were
    weaker shapes for the same problem: keep the same bytes from uploading
    twice into the same Drive folder, no matter which thread they arrive in.

    Key shape:
      - `content_sha1` identifies the bytes (sender-independent).
      - `drive_folder_path` identifies the destination ("Notion Email
        Attachments/Prosjekt/2026/Acme"). Different folder = separate row, so
        each project's Drive folder stays self-contained — a teammate browsing
        a project's attachments sees a complete picture.

    Read at the start of each thread sync to seed the in-memory tracker;
    written once per successful Drive upload. A re-sync of the same bytes to
    the same folder finds the row, reuses `drive_url`, and skips the upload.
    """

    __tablename__ = "attachment_blobs"

    content_sha1: Mapped[str] = mapped_column(String(40), primary_key=True)
    drive_folder_path: Mapped[str] = mapped_column(String(1024), primary_key=True)
    drive_name: Mapped[str] = mapped_column(String(255), nullable=False)
    drive_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # First filename we saw these bytes under — debugging hint only; Gmail
    # renumbers inline image names across messages even for identical bytes.
    first_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ContactSignatureImage(Base):
    """Per-contact byte-exact image signatures, learned by repetition across threads.

    Catches the long-tail of signature images that the structural rule in
    `_partition_attachments` misses: a sender's MUA attached the logo as plain
    `Content-Disposition: attachment` with no `cid:` reference, so it's
    indistinguishable from a real attachment until we notice it keeps appearing.

    Counter shape: increments by ONE per distinct (sender_email, content_sha1,
    gmail_thread_id). A re-carry within the same thread doesn't bump (Outlook
    re-quotes the bytes on every reply — `last_thread_id == current` is the
    guard). Past `settings.signature_learn_threshold` the upload loop starts
    skipping the bytes for that sender forever. Keyed on sha1 (not filename),
    so a real screenshot named the same as a logo is byte-different and never
    collides.

    `status` lifecycle:
      - "learning"   counting; uploads as normal.
      - "signature"  at/past threshold; future emails skip these bytes.
      - "allowlisted" user-marked "always upload"; never skipped, never bumped.
    """

    __tablename__ = "contact_signature_images"

    sender_email: Mapped[str] = mapped_column(String(254), primary_key=True)
    content_sha1: Mapped[str] = mapped_column(String(40), primary_key=True)
    thread_seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    first_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="learning")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProjectLabel(Base):
    """Notion project page ↔ Gmail label, one row per (project, user).

    Lets us rename a Gmail label by ID after the user renames the project in
    Notion. Without this mapping the link between the two sides is name-only,
    which silently breaks the moment someone renames a project.
    """

    __tablename__ = "project_labels"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_email: Mapped[str] = mapped_column(String(254), primary_key=True)
    gmail_label_id: Mapped[str] = mapped_column(String(64), nullable=False)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # PK is (notion_page_id, user_email) in that order, so single-column lookups
    # by user_email can't use it. The Gmail webhook filters every push against
    # `WHERE user_email = ?` — keep that hot path on a real index. The composite
    # below additionally serves the per-thread project match in sync_thread
    # (`WHERE user_email = ? AND gmail_label_id IN (...)`); its leading column
    # also covers the plain user_email lookup, but the standalone index is kept
    # for clarity of intent.
    __table_args__ = (
        Index("ix_project_labels_user_email", "user_email"),
        Index("ix_project_labels_user_label", "user_email", "gmail_label_id"),
    )


class ProjectFolder(Base):
    """Notion project page ↔ folder on the office NAS, one row per project.

    Analogous to ProjectLabel, but the host filesystem is single (not per-user),
    so the project page ID alone is the key. Stores the last-written path/name
    so a Notion rename can move the folder in place instead of orphaning it.
    """

    __tablename__ = "project_folders"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    current_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaskFolder(Base):
    """Notion task ("Oppgaver") page ↔ folders on the office NAS.

    A task is one Notion row that ends up as one folder under each of the
    project's 5 discipline-conditional parents matching the task's discipline.
    We don't store the paths themselves — they're derived on each sync from the
    project's current name/created_time and the task's current discipline. We
    DO store `current_name` and `current_discipline` so a rename or discipline
    change can be detected and moved in place rather than orphaned.

    The project_page_id is denormalized for indexed lookups (e.g. "all tasks
    for project X" when a project is renamed in the future).
    """

    __tablename__ = "task_folders"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_page_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_discipline: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FrameProjectFolder(Base):
    """Notion project page ↔ Frame.io Project, one row per project.

    Each Notion project provisions its own top-level Frame Project under the
    configured workspace (frame_workspace_id). The Project's auto-created
    root_folder_id is the parent under which discipline + task folders nest.

    Two distinct Frame ids live here:
      - frame_project_id: the Project entity itself. What we rename via
        PATCH /projects/{id} and (future work) toggle active/inactive on.
      - frame_folder_id: the Project's root_folder_id. The parent we pass
        to create_folder when provisioning discipline subfolders.

    Both are stable across Notion renames — rename_project preserves both
    ids — so any per-task placeholder files underneath keep their ids
    (which Phase 2 comment polling will key on).

    `frame_url` is cached so the worker doesn't recompute it on idempotent
    runs and Phase 2 can attach a correction back to a known URL without
    re-resolving.
    """

    __tablename__ = "frame_project_folders"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # The Frame Project id (top-level entity in the workspace). What we
    # rename/archive/inactivate. Distinct from frame_folder_id because Frame's
    # Project entity exposes its own endpoints (PATCH /projects/{id}, archive,
    # active/inactive flag) that don't apply to folders.
    frame_project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The Project's root_folder_id — the parent under which discipline and
    # task folders live. Returned at project create/get time; stable for the
    # lifetime of the Project.
    frame_folder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    frame_url: Mapped[str] = mapped_column(String(512), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FrameLeveranseFolder(Base):
    """Notion deliverable row ↔ Frame.io placeholder file (under its discipline folder).

    `notion_page_id` is a DELIVERABLE row in the Oppgaver DB (Type is a
    recognized discipline — Interiør/Eksteriør/Animasjon/Annet). The Oppgaver
    DB also holds internal-task rows (e.g. Type="Klargjøre modell") and
    Korreksjonsrunde sub-rows, but only deliverables ever get a cache row
    here — the webhook gate filters the rest out before the Frame sync runs.
    Each deliverable provisions a V00 placeholder file under the project's
    discipline subfolder; that file becomes the base of Frame's version
    stack (V01 = first real delivery, V02 = revision after round 1, etc.).

    Layout shift (post-flatten): there is no longer a per-leveranse wrapping
    folder. The placeholder file sits directly under the discipline folder,
    and the filename (`<project>_<studio>_<leveranse>_V00.jpg`) IS the visible
    label in Frame's UI. As a consequence:
      - `frame_folder_id` now holds the SHARED discipline folder id —
        multiple FrameLeveranseFolder rows in the same discipline-of-project
        legitimately reference the same id here. Self-heal does NOT key off
        this column anymore (a missing discipline folder isn't a per-leveranse
        signal); the load-bearing per-leveranse anchor is the placeholder
        file id.
      - `frame_placeholder_file_id` is the per-leveranse anchor and is what
        the stale-check, comment sync, and version sync all key on.

    The placeholder file id makes "first delivery uploads as V01 on top" work
    in Frame's UI; we persist it so:
    (a) Phase 2 comment polling joins comment.file_id → this row →
        Notion Leveranse page, and
    (b) a Leveranse rename preserves the placeholder rather than
        recreating it (and losing the version stack). Under the flattened
        layout, a Notion rename also PATCHes the file's name in Frame so
        the visible label stays in sync.

    `project_page_id` denormalized for "all Frame folders for project X"
    lookups (e.g. when a project's folder id is re-resolved after
    self-heal). Column names are kept as-is across the
    frame_task_folders → frame_leveranse_folders table rename and across
    the post-flatten semantic shift — the row meaning evolved, the column
    storage didn't.
    """

    __tablename__ = "frame_leveranse_folders"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_page_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    frame_folder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    frame_placeholder_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    current_discipline: Mapped[str] = mapped_column(String(64), nullable=False)
    frame_url: Mapped[str] = mapped_column(String(512), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FrameComment(Base):
    """Frame.io comment ↔ Notion Korreksjon row.

    One row per Frame comment we've successfully synced. The primary key is
    the Frame comment UUID; INSERTs use ON CONFLICT DO NOTHING for engine-
    level idempotency (a re-delivered webhook for the same comment id is
    a no-op).

    Column meanings after the DB restructure (Oppgaver + Korreksjoner):
      - `leveranse_page_id` = the DELIVERABLE row's page id (in the Oppgaver
        DB). Resolved via the cached placeholder file id, unchanged by the
        restructure.
      - `oppgave_page_id` = the per-comment KORREKSJON row's page id (in the
        Korreksjoner DB). A reply's parent lookup reads the parent comment's
        `oppgave_page_id` to set the new reply's Parent item relation.

    `notion_block_id` is legacy (the Phase 2 bullet-block model). Unused in
    the per-row model — replies are separate Korreksjon rows, nested via the
    Parent item relation, so there's no per-bullet block to track. Kept
    nullable for old rows.

    `parent_comment_id` is set when this comment is a reply (the parent's
    `replies: [...]` array surfaces it). Frame's reply object itself does
    NOT carry a parent pointer — we derive it from where it sits in the
    response tree when fetching the parent.

    `round_number` is the Frame file version number (V01 = round 1,
    V00 = round 0). Cached here so we don't re-derive it on every reply.

    `body_snippet` is the first 512 chars of the comment text — kept for
    operator debugging at /debug/queue and for log lines; the canonical
    body lives in Frame and (rendered) in Notion.
    """

    __tablename__ = "frame_comments"

    frame_comment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    frame_file_id: Mapped[str] = mapped_column(String(64), nullable=False)
    leveranse_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    oppgave_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_comment_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notion_block_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    body_snippet: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        # "Replies for this file + round" — used when a reply needs to
        # find its parent's notion_block_id to indent under.
        Index("ix_frame_comments_file_round", "frame_file_id", "round_number"),
        # "All comments on this Oppgave" — used by operator-side rebuilds
        # and by the engine when a Korreksjonsrunde row needs to enumerate
        # what's already there.
        Index("ix_frame_comments_oppgave", "oppgave_page_id"),
    )


class FrameKorreksjonsrunde(Base):
    """Notion "Korreksjonsrunde N" row ↔ its (deliverable, round) key.

    Dedup cache for the lazily-created round sub-rows. The first Frame
    comment of round N under a deliverable creates the "Korreksjonsrunde N"
    row in the Oppgaver DB; this table records `(leveranse_page_id,
    round_number) → notion_page_id` so the *next* comment of the same round
    finds the existing row from Postgres instead of querying Notion.

    Why this can't ride on a Notion query alone: Notion's
    `/databases/{id}/query` index is eventually consistent, lagging page
    creation by seconds. When two comments of a fresh round arrive in a
    burst (the normal review pattern), the second comment's query doesn't
    yet see the round row the first one created → it creates a duplicate.
    Postgres is read-your-writes, so caching the create here closes the
    race. Notion stays the source of truth; this is pure dedup (same
    contract as ContactCache / FrameLeveranseFolder).

    Self-heal: if the cached page id is later found archived/deleted in
    Notion, the read path evicts the row and re-resolves (falling back to
    the Notion query, then create).
    """

    __tablename__ = "frame_korreksjonsrunder"

    leveranse_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    round_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TogglProject(Base):
    """Notion project page ↔ Toggl Track project, one row per project.

    Toggl allows duplicate-named projects in a workspace, so name lookups
    aren't safe — the integration MUST own the mapping. This table is that
    mapping: a Notion project page id resolves to the Toggl numeric project
    id (plus the workspace it lives in and the URL the team clicks from
    Notion to land on it).

    Self-healing parallels FrameProjectFolder: if `toggl_project_id`
    starts returning 404/403 (admin deleted the project in Toggl's UI),
    the engine evicts this row and re-creates on the next pass.

    `toggl_workspace_id` is denormalized so a sync engine doesn't need to
    re-read settings to know which workspace the project belongs to (and
    so a future workspace migration is a single UPDATE rather than a
    schema change).
    """

    __tablename__ = "toggl_projects"

    notion_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Toggl's project ids are int64; stored as string to match every other
    # external id in this schema and to avoid integer-overflow surprises
    # if Toggl ever widens the type.
    toggl_project_id: Mapped[str] = mapped_column(String(32), nullable=False)
    toggl_workspace_id: Mapped[str] = mapped_column(String(32), nullable=False)
    current_name: Mapped[str] = mapped_column(String(255), nullable=False)
    toggl_url: Mapped[str] = mapped_column(String(512), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TogglUserCache(Base):
    """Toggl workspace user roster, cached from Toggl's API.

    The hours engine needs an email for each Toggl user_id so it can map
    that user to a native Notion account (by email match against
    `GET /v1/users`). Time-entry payloads from Reports v3 only carry
    `user_id`; this table is what makes id → email a single PK read.

    Refreshed at the top of every nightly hours sync via
    `sync.refresh_toggl_users_cache.refresh_toggl_users_cache` (new
    workspace members get picked up on the next run without a restart).
    Rows whose toggl_user_id no longer appears in the workspace get
    deleted, so a departed employee stops showing up in lookups.

    No UNIQUE on email: Toggl deduplicates by user_id, not email, and
    emails are mutable. The engine's per-run Notion-side index is the
    one place where we assume "one email = one Notion user."
    """

    __tablename__ = "toggl_user_cache"

    toggl_user_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), nullable=False)
    # Cached display name for logs — the canonical value lives in Toggl.
    name: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TimerDbCache(Base):
    """Year → Notion DB ID for the year-partitioned Timer databases.

    Direct parallel to EmailsDbCache. The Timer year router
    (`clients/notion_timer_db.py`) auto-creates `Timer YYYY` databases on
    first sync of each year and caches the resolved id here so we don't
    search Notion on every nightly run. Cached id is self-healed if Notion
    no longer has the DB (deleted/recreated by hand): the next resolve
    evicts and recreates.
    """

    __tablename__ = "timer_db_cache"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    notion_db_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailsDbCache(Base):
    """Year → Notion DB ID for the year-partitioned Emails databases.

    The year router (`clients/notion_emails_db.py`) auto-creates `Emails YYYY`
    databases on first sync of each year. This table caches the resolved IDs
    so we don't search Notion on every webhook. On startup the router does a
    one-time refresh from Notion to catch DBs created by other instances or
    by hand. The webhook feedback-loop filter reads this table to recognize
    "our own writes" across all year DBs without per-DB env config.
    """

    __tablename__ = "emails_db_cache"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    notion_db_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class FikenInvoice(Base):
    """One Fiken invoice (draft or sent) that we've touched.

    Two roles in one table:

    1. **Creation audit trail** — every time the Notion creation engine
       successfully POSTs a draft to Fiken, it writes one row here BEFORE
       touching Notion. If the Notion stamp write fails (Oppstartsfakturert
       andel + Skal-checkboxes), the next run sees the audit row and skips
       — so a flaky network during the Notion call can't double-bill.
    2. **Poller dedup cache** — when the scheduled `fiken_poll` task pulls
       a sent invoice from Fiken, this row keeps the resolved Notion page
       id (in the `Fakturaer YYYY` DB) for cheap "did we already mirror this
       invoice?" lookups. `last_modified_date` lets us skip unchanged rows.

    Why both roles share the table: an invoice we created via the button
    also gets seen by the poller once the user clicks Send in Fiken. The
    PK (company_slug, fiken_invoice_id) is the stable identity in both
    directions; the optional columns track whichever role wrote first.

    `invoice_type` is "oppstart" / "slutt" for our own creations, NULL
    for poller-discovered invoices (we don't know whether a hand-created
    Fiken invoice is a startup or final invoice). `oppstart_fraction` is
    the per-row fraction applied at creation time (e.g. 0.5 for a 50%
    startup invoice), persisted so a re-run of the slutt engine reads
    truth from here OR from Notion and they always agree.
    """

    __tablename__ = "fiken_invoices"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    fiken_invoice_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # The Notion project this invoice belongs to. Resolved by the creation
    # engine (it IS the source) or by the poller (matched via the reference
    # field that carries the project title — replaces the Make automation).
    # Nullable because the poller may legitimately not match a project on
    # a hand-created invoice; the operator hand-links in Notion.
    project_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # The Notion page id in `Fakturaer YYYY`, written by the poller. NULL on
    # rows written only by the creation engine until the poller first sees it.
    notion_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # "oppstart" / "slutt" for our own creations; NULL for poller-discovered.
    invoice_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # The fraction applied per ticked row at creation time. NULL on poller-
    # discovered rows. 0.5 for a default 50% startup invoice.
    invoice_fraction: Mapped[float | None] = mapped_column(nullable=True)
    # Fiken's lastModifiedDate (string yyyy-mm-dd) — poll cursor reads this
    # to skip unchanged rows on incremental polls.
    last_modified_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Phase C.3a — set at draft creation. Lets the poller match a sent
    # Fiken invoice to this row via Fiken's `invoiceDraftUuid` field
    # (the SENT invoice's `invoiceId` is freshly minted at send-time,
    # so we can't match on id; but the draft's `uuid` is preserved
    # as `invoiceDraftUuid` on the sent record). NULL on poller-
    # discovered rows (those never had a draft we knew about).
    draft_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Phase C.3a — graduation marker. Set when the poller / manual
    # trigger successfully reconciles this row to a sent invoice in
    # Fiken. NULL = the draft is still in Fiken awaiting Send (or the
    # poller hasn't processed it yet). Lets `/debug/fiken/sent-invoices`
    # show what we've already graduated vs what's still pending.
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Phase C.3a — the Fiken UI URL we wrote into the Faktura DB row's
    # URL column. Captured here for diagnostics so we don't have to
    # walk back through Notion to find it.
    sent_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Phase C.3a — Fiken's `invoiceNumber` on the sent invoice (the
    # printable number like "10042"). NULL until graduated.
    invoice_number: Mapped[str | None] = mapped_column(String(32), nullable=True)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class FikenInvoiceLine(Base):
    """One Oppgave row billed on one Fiken invoice — the per-row audit trail.

    Written by the creation engine after a successful POST. Lets us answer
    "what was billed on the startup invoice?" without re-querying Fiken,
    so the slutt engine can validate its remainder math against persisted
    truth (a second cross-check on top of `Oppstartsfakturert andel` on the
    Notion row itself).

    PK is (company_slug, fiken_invoice_id, oppgave_page_id) — the same
    Oppgave can appear on both an oppstart and a slutt invoice as separate
    rows in this table (different fiken_invoice_id). `discipline` and
    `unit_price_ore` (NOK øre, integer cents) are captured at creation
    time so a later price edit on the Notion row doesn't rewrite history.
    """

    __tablename__ = "fiken_invoice_lines"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    fiken_invoice_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    oppgave_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Nullable: rows whose Type isn't a recognized discipline (free-text /
    # internal tasks) are billable now and land on the draft with discipline=None.
    discipline: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # NOK amount actually billed on THIS invoice line, in øre (integer cents)
    # so float drift is impossible. The cumulative sum across all of an
    # Oppgave's lines equals what Notion's `Fakturert beløp` holds (modulo
    # the øre→NOK conversion). v1 stored a fraction here; v2 stores the
    # absolute amount because Pris is mutable (operators can renegotiate
    # between oppstart and slutt) — a fraction would be meaningless once
    # the underlying Pris changes.
    billed_amount_ore: Mapped[int] = mapped_column(Integer, nullable=False)
    # Mirrors billed_amount_ore — kept under both column names while v1
    # callers still reference unit_price_ore. New writes set both to the
    # same value; eventual cleanup is to drop unit_price_ore once nothing
    # reads it.
    unit_price_ore: Mapped[int] = mapped_column(Integer, nullable=False)
    inserted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        Index("ix_fiken_invoice_lines_oppgave", "oppgave_page_id"),
    )


class FakturaNotionCache(Base):
    """Idempotency cache for Phase C.2 Faktura DB writes.

    Each row records "we created a Notion page in the Faktura DB for
    this Fiken record." Phase C.3's poller checks this cache before
    writing — a cached hit is a no-op, so a repeated poll over the
    same window doesn't create duplicate Notion rows.

    PK is (company_slug, record_type, fiken_record_id) where record_type
    is "faktura" or "kreditnota" — the two record types share the
    same Faktura DB in Notion but live at different Fiken endpoints
    (`/invoices` vs `/creditNotes`), and their ids COULD collide
    numerically since they're separate sequences in Fiken.

    `notion_page_id` is recorded purely for diagnostics / debug
    endpoints. The cache never tries to re-PATCH an existing page;
    the operator is free to delete a Faktura row in Notion intentionally
    (e.g. they corrected something) and the cache will silently leave
    it alone on subsequent polls. Notion is the source of truth for
    "does this row exist or not" — Postgres just remembers we already
    wrote it once.

    Make.com automation rows are NOT tracked here. Phase C migration
    expects Make to keep populating the DB until C.3 ships; the two
    writers coexist because they target different sets of records
    (Make handles whatever it was configured for; we only write rows
    we've matched ourselves). Final cutover happens at C.4.
    """

    __tablename__ = "faktura_notion_cache"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    record_type: Mapped[str] = mapped_column(String(16), primary_key=True)
    fiken_record_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    notion_page_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "record_type IN ('faktura', 'kreditnota')",
            name="faktura_notion_cache_record_type_check",
        ),
    )


class FikenProductByKategori(Base):
    """Maps a Notion `Oppgave kategori` multi-select label to a Fiken
    product id, cached per `company_slug`.

    The Fiken creation engine looks up the Kategori label in this cache
    on each invoice line; on miss it lists Fiken's products, finds the
    one whose `name` matches the label, and upserts the mapping. Send
    faktura then includes `productId` on the line so Fiken's `description`
    and `incomeAccount` auto-fill from the product itself — operators
    manage the catalogue + account routing in Fiken's UI without the
    engine knowing anything about kontonummer.

    On a name-match miss the engine sends the line free-text with the
    Kategori label as `description`. Fiken rejects free-text lines with
    no `incomeAccount` (400 `incomeAccount is required for free-text
    lines …`), so the row surfaces as a failure with a clear message —
    the operator adds the missing product in Fiken before re-clicking.

    `name_when_cached` records the Fiken product name at insertion time
    so an operator-renamed product in Fiken is visible in the cache row
    on the next debug pull (the engine still uses the cached id, which
    survives a Fiken-side rename).
    """

    __tablename__ = "fiken_product_by_kategori"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    kategori_label: Mapped[str] = mapped_column(String(128), primary_key=True)
    fiken_product_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name_when_cached: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FikenPlaceholderContact(Base):
    """One row per Fiken company_slug: the contactId of the "Mangler kunde"
    (missing-customer) placeholder we link drafts to when the project's
    Fakturamottaker has no Orgnr yet.

    Auto-created on first sighting — the engine creates a Fiken contact
    with name = settings.fiken_placeholder_contact_name and no Orgnr,
    then caches the id here so subsequent missing-Orgnr runs reuse the
    same contact instead of cluttering Fiken with one orphan customer
    per run.

    Single source of truth: there is exactly one placeholder per company.
    Renaming the placeholder in Fiken is harmless — we key on the cached
    id, not the name. If the operator deletes it in Fiken the cache row
    self-heals on next use (404 → create + upsert).
    """

    __tablename__ = "fiken_placeholder_contacts"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    fiken_contact_id: Mapped[str] = mapped_column(String(32), nullable=False)
    name_when_created: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FikenBankAccount(Base):
    """One row per Fiken company_slug: the `bankAccountNumber` we send on
    every draft so the printed invoice shows a Kontonummer (not blank).

    Auto-resolved on first send_faktura per company — the engine calls
    `fiken_client.list_bank_accounts(company_slug)`, picks the first
    active normal-type account, and caches its bankAccountNumber here.
    Subsequent runs read the cache; no per-deploy config needed.

    The operator can override via `FIKEN_BANK_ACCOUNT_NUMBER` env var.
    When set, the cache is bypassed entirely and the env value flows
    through. Useful when the company has multiple accounts and the
    operator wants to pin one explicitly.

    Self-heal: if Fiken rejects the cached number (deleted on Fiken's
    side, made inactive), we re-resolve on the next run. The engine
    treats a 400 from create_invoice_draft mentioning the bank account
    as the signal to evict the cache row and re-pick.

    Field empirically pinned: `bankAccountNumber` is the field Fiken
    accepts and echoes back on draft creation; `bankAccountCode` and
    `bankAccountId` are silently dropped.
    """

    __tablename__ = "fiken_bank_accounts"

    company_slug: Mapped[str] = mapped_column(String(64), primary_key=True)
    bank_account_number: Mapped[str] = mapped_column(String(32), nullable=False)
    name_when_cached: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class FakturaerDbCache(Base):
    """Year → Notion DB ID for the year-partitioned Fakturaer databases.

    Direct parallel to EmailsDbCache / TimerDbCache. The Fiken year router
    (`clients/notion_fiken_db.py`) auto-creates `Fakturaer YYYY` databases
    on first sighting of an invoice in that year and caches the resolved
    id here. Self-heal: a cached id that no longer exists in Notion is
    evicted and the DB re-created on the next resolve.
    """

    __tablename__ = "fakturaer_db_cache"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    notion_db_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class TilbudDbCache(Base):
    """Year → Notion DB ID for the year-partitioned Tilbud databases.

    Same shape as FakturaerDbCache; offers (tilbud) and invoices live in
    separate year DBs so views/filters don't have to discriminate by a
    type column.
    """

    __tablename__ = "tilbud_db_cache"

    year: Mapped[int] = mapped_column(Integer, primary_key=True)
    notion_db_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# Status values for SyncTask. Kept as a plain string + CHECK (not a Postgres
# ENUM) because ENUMs are painful to extend via Alembic and the rest of the
# schema uses plain strings/booleans.
SYNC_TASK_STATUSES = ("pending", "in_progress", "done", "failed")

# What unit of work a row represents. 'thread' is the original (and default):
# one Gmail thread → Notion rows via sync_thread. 'label_sync' is "reconcile +
# create this project's Gmail label across every active mailbox" — a different
# unit (keyed on project, no thread). 'nas_folder_sync' provisions the project
# folder structure on the office NAS (split out of label_sync so each system
# has its own retry/failure surface and its own per-system button).
# 'task_folder_sync' provisions NAS folders for one Notion task page (Oppgaver
# DB). 'frame_project_sync' and 'frame_leveranse_sync' do the same for
# Frame.io — kept separate from the NAS task types because Frame I/O is
# slower (placeholder upload) and gated by its own SYNC_FRAME toggle, so
# a Frame outage shouldn't tie up NAS/label retries.
# 'frame_comment_sync' is the Phase 2 inbound side: a Frame webhook fires
# on comment.created / comment.updated / comment.completed / comment.uncompleted,
# the receiver enqueues one of these per comment id, and the worker fetches
# the full comment and (a) creates a Korreksjon sub-Oppgave under its
# Korreksjonsrunde and (b) for completed/uncompleted events propagates the
# done state into the Notion checkbox.
# 'frame_version_sync' is the Phase 2.5 trigger for file.versioned events:
# a new version uploaded on top of the placeholder flips the Leveranse
# status to "Ferdig". gmail_thread_id carries the Frame version_stack id.
# 'oppgave_done_sync' is the Phase 2.5 Notion-side propagator: a Notion
# automation fires when the team toggles a Korreksjon's Ferdig checkbox;
# the engine PATCHes the matching Frame comment's completed state.
# 'leveranse_status_recheck' rolls up the active round's Korreksjon
# completion counts into Under arbeid / Oppgaver ferdig.
# 'toggl_project_sync' mirrors a Notion Project to Toggl Track (Phase 1
# of the Toggl integration). The Notion Initialize button fans out to this
# task type alongside label_sync / nas_folder_sync / frame_project_sync
# when SYNC_TOGGL=true. project_page_id carries the Notion page id; the
# engine looks up the existing TogglProject cache row (or creates one)
# and renames the Toggl project if the title changed.
# 'toggl_hours_sync' is the Phase 2 nightly aggregator. APScheduler
# enqueues one of these per day; the engine pulls the last 14 days from
# Toggl Reports v3, aggregates per (user, project, local_day), and
# upserts rows into the year-partitioned `Timer YYYY` Notion DB. There's
# at most one active row at a time — the dedup index uses a fixed
# sentinel value in gmail_thread_id so a late-night double-trigger
# collapses to one run.
# New types go here + the CHECK below + a branch in jobs/queue_worker._process.
SYNC_TASK_TYPES = (
    "thread",
    "label_sync",
    "nas_folder_sync",
    "task_folder_sync",
    "frame_project_sync",
    "frame_leveranse_sync",
    "frame_comment_sync",
    "frame_version_sync",
    "frame_file_status_sync",
    "oppgave_done_sync",
    "leveranse_status_recheck",
    "toggl_project_sync",
    "toggl_hours_sync",
    # Fiken Phase B (v2). `send_faktura` is fired by the per-project
    # "Send faktura" Notion button; gmail_thread_id is the project_page_id
    # alone (the engine reads the project's `Faktura status` to decide
    # oppstart vs slutt). `fiken_poll` is the scheduled global singleton
    # — at most one active ever, mirroring toggl_hours_sync.
    "send_faktura",
    "fiken_poll",
)

# Sentinel value stuffed into gmail_thread_id on toggl_hours_sync rows so
# the partial unique index `uq_sync_tasks_active_toggl_hours` collapses
# every active row to one — there is at most one global hours sync in
# flight. Exposed as a constant so the enqueue helper and the migration
# downgrade both reference the same string.
TOGGL_HOURS_SINGLETON_KEY = "toggl-hours-singleton"

# Same idea for the Fiken scheduled poller: every active row carries this
# sentinel in gmail_thread_id so uq_sync_tasks_active_fiken_poll collapses
# any concurrent enqueue attempts (cron + manual /debug/fiken/poll) to one.
FIKEN_POLL_SINGLETON_KEY = "fiken-poll-singleton"


class SyncTask(Base):
    """Durable work queue: one Gmail thread that must be synced into Notion.

    This is the crash-safe heart of the guarantee "a thread under a project
    label WILL reach Notion." The Gmail webhook only ENQUEUES rows here (in the
    same transaction as the history cursor advance); a background worker claims
    `pending` rows, runs `sync_thread`, and marks them `done` or `failed`.
    Because the work lives on disk, a crash/restart loses nothing — pending rows
    are still here, and a row left `in_progress` by a dead process is reset to
    `pending` on boot.

    One *active* row per (user_email, gmail_thread_id) — enforced by the partial
    unique index below. After a row reaches `done`/`failed` a fresh row may be
    enqueued for the same thread (e.g. a reply months later); `sync_thread`'s
    Notion-backed dedup means re-running only appends the new messages.
    """

    __tablename__ = "sync_tasks"

    # Surrogate PK gives the claim query a stable tiebreaker for FIFO ordering.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # What kind of work this row is. Defaults to 'thread' so every existing
    # enqueue path (gmail push, resync buttons) keeps working unchanged. A
    # 'label_sync' row syncs a project's Gmail label across all mailboxes; it has
    # no real thread, so user_email/gmail_thread_id below are NOT meaningful for
    # it — only project_page_id is. The worker dispatches on this column.
    task_type: Mapped[str] = mapped_column(String(32), nullable=False, server_default="thread")
    # For a 'thread' row these identify the work. For a 'label_sync' row they are
    # unused placeholders (the active-dedup index for label_sync keys on
    # project_page_id instead — see uq_sync_tasks_active_label below); we still
    # fill them with the project page id / "*" because the columns are NOT NULL.
    user_email: Mapped[str] = mapped_column(String(254), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    # When true the worker REBUILDS (archive existing rows + recreate fresh under
    # current code) instead of a plain repair-in-place sync. Set by the per-email
    # "Re-sync" button; a normal Gmail-push enqueue leaves it false.
    rebuild: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Resolved lazily by the worker once sync_thread reads the thread's labels
    # (null until then, and for threads that match no project). Lets the Projects
    # DB status dot answer "any active/failed task for project X?" without
    # re-deriving the project. A thread can map to several projects; we record
    # the primary one (first match) — enough for an at-a-glance per-project dot.
    project_page_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Backoff gate: a failed row becomes claimable again only once now() passes
    # this. One column drives both "claim now" (pending) and "retry later".
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','in_progress','done','failed')",
            name="ck_sync_tasks_status",
        ),
        CheckConstraint(
            "task_type IN ('thread','label_sync','nas_folder_sync',"
            "'task_folder_sync','frame_project_sync','frame_leveranse_sync',"
            "'frame_comment_sync','frame_version_sync',"
            "'frame_file_status_sync','frame_project_status_sync',"
            "'oppgave_done_sync',"
            "'leveranse_status_recheck','toggl_project_sync',"
            "'toggl_hours_sync','project_status_dispatch',"
            "'fiken_invoice_create','send_faktura','fiken_poll')",
            name="ck_sync_tasks_task_type",
        ),
        # At most one ACTIVE (pending/in_progress) row per thread — this is the
        # dedup that makes re-enqueue on every push/reply a safe no-op. The
        # partial predicate still allows a new row once the prior one is
        # done/failed (so a later reply re-enqueues the thread). Scoped to
        # task_type='thread' so label_sync rows (which reuse the thread columns
        # as placeholders) don't collide here.
        Index(
            "uq_sync_tasks_active_thread",
            "user_email",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text("status IN ('pending','in_progress') AND task_type = 'thread'"),
        ),
        # At most one ACTIVE label_sync per project — so a double-click (or 5 fast
        # clicks on the same project) collapses to one task, while distinct
        # projects each get their own. The thread index above can't serve this:
        # label_sync rows share placeholder thread-column values.
        Index(
            "uq_sync_tasks_active_label",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'label_sync'"
            ),
        ),
        # At most one ACTIVE nas_folder_sync per project. Mirrors the label_sync
        # dedup; collapsing both task types on `project_page_id` is fine because
        # each index is scoped to its own task_type via the partial predicate,
        # so a project can have one active label_sync AND one active
        # nas_folder_sync simultaneously without collision.
        Index(
            "uq_sync_tasks_active_nas_folder",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'nas_folder_sync'"
            ),
        ),
        # At most one ACTIVE frame_project_sync per project (same dedup intent
        # as label_sync — a Notion webhook re-fire on the same Project page must
        # collapse to one task even though both task types may be in flight at
        # the same time for different reasons).
        Index(
            "uq_sync_tasks_active_frame_project",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_project_sync'"
            ),
        ),
        # At most one ACTIVE frame_project_status_sync per project. A Notion
        # `Status` flip from Ferdig → Tapt (or vice versa, or toggling out
        # and back in) within the engine's run window would otherwise enqueue
        # multiple tasks for the same project; the engine reads the live
        # Notion status at process time, so collapsing same-id is correct.
        # Independent of frame_project_sync — provisioning (folder + V00) and
        # active/inactive lifecycle can both be in-flight for one project.
        Index(
            "uq_sync_tasks_active_frame_project_status",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_project_status_sync'"
            ),
        ),
        # At most one ACTIVE project_status_dispatch per project. The webhook
        # is a tiny "ack + enqueue this one row" handler — the dispatch task
        # does the slow work (Notion fetch, placeholder check, status read,
        # fan-out to every provisioning engine). Collapsing rapid Status
        # flips (Ferdig → Tapt → Ferdig within a few seconds) to one task is
        # correct: the dispatcher reads live Notion state at process time, so
        # the last-flipped state is what gets propagated. Reason for splitting
        # this from frame_project_status_sync: dispatch fans out to ALL the
        # engines (gmail/nas/frame/toggl + the frame status sync); the latter
        # is just one of those fan-out targets.
        Index(
            "uq_sync_tasks_active_project_status_dispatch",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'project_status_dispatch'"
            ),
        ),
        # At most one ACTIVE frame_leveranse_sync per Leveranse (renamed
        # from frame_task_sync in Phase 2; the column rename + index
        # predicate update happen in the same Alembic migration as the
        # frame_task_folders → frame_leveranse_folders rename).
        # Mirrors the task_folder_sync pattern: gmail_thread_id carries
        # the Leveranse page id as a placeholder so the dedup is on a
        # single column.
        Index(
            "uq_sync_tasks_active_frame_leveranse",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_leveranse_sync'"
            ),
        ),
        # At most one ACTIVE frame_comment_sync per Frame comment. The
        # comment UUID is stashed in gmail_thread_id (mirroring the
        # task_folder_sync / frame_leveranse_sync pattern); a webhook
        # redelivery for the same comment id while a sync is still
        # in-flight collapses to a single task.
        Index(
            "uq_sync_tasks_active_frame_comment",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_comment_sync'"
            ),
        ),
        # Phase 2.5 — At most one ACTIVE frame_version_sync per version
        # stack. gmail_thread_id carries the Frame version_stack id (from
        # the file.versioned webhook's resource.id). A rapid sequence of
        # file.created/.upload.completed/.ready/.versioned events that
        # could end up enqueueing more than one task for the same stack
        # collapses to a single row here.
        Index(
            "uq_sync_tasks_active_frame_version",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_version_sync'"
            ),
        ),
        # Utgår reconcile — at most one ACTIVE frame_file_status_sync per
        # Leveranse. gmail_thread_id carries the Notion deliverable page
        # id (the reconcile engine reads both Notion + Frame at process
        # time, so collapsing simultaneous Notion-side + Frame-side
        # nudges to one task is correct).
        Index(
            "uq_sync_tasks_active_frame_file_status",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'frame_file_status_sync'"
            ),
        ),
        # Phase 2.5 — At most one ACTIVE oppgave_done_sync per Oppgave
        # page. gmail_thread_id carries the Notion Oppgave page id. The
        # Notion automation may fire rapidly when the team toggles
        # checkboxes; dedup keeps the engine running once per row.
        Index(
            "uq_sync_tasks_active_oppgave_done",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'oppgave_done_sync'"
            ),
        ),
        # Phase 2.5 — At most one ACTIVE leveranse_status_recheck per
        # Leveranse. gmail_thread_id carries the Leveranse page id. The
        # cascade of N children being toggled in succession all enqueue
        # the same parent's recheck task; the dedup makes it a single
        # rollup pass.
        Index(
            "uq_sync_tasks_active_leveranse_status",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'leveranse_status_recheck'"
            ),
        ),
        # Toggl Phase 1 — At most one ACTIVE toggl_project_sync per project
        # (mirrors uq_sync_tasks_active_frame_project: a Notion button
        # re-fire on the same Project page must collapse to one task even
        # though label_sync / nas_folder_sync / frame_project_sync /
        # toggl_project_sync can all be in flight for the same project at
        # the same time).
        Index(
            "uq_sync_tasks_active_toggl_project",
            "project_page_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'toggl_project_sync'"
            ),
        ),
        # Toggl Phase 2 — At most one ACTIVE toggl_hours_sync ever (global
        # singleton: there's only one nightly aggregator, not per-project).
        # The enqueue helper stuffs TOGGL_HOURS_SINGLETON_KEY into
        # gmail_thread_id so every active row collides on that single
        # value, collapsing a double-trigger (cron + manual /debug call,
        # or two cron pulses if APScheduler ever fires twice) to one run.
        Index(
            "uq_sync_tasks_active_toggl_hours",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'toggl_hours_sync'"
            ),
        ),
        # Fiken Phase B (v2) — At most one ACTIVE send_faktura per project.
        # The enqueue helper stuffs project_page_id alone into
        # gmail_thread_id (no oppstart/slutt suffix — the engine reads
        # the project's `Faktura status` to decide), so any double-click
        # of the "Send faktura" button while the previous run is in
        # flight collapses to one row.
        Index(
            "uq_sync_tasks_active_send_faktura",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'send_faktura'"
            ),
        ),
        # Fiken Phase A — At most one ACTIVE fiken_poll ever (global
        # singleton, mirrors uq_sync_tasks_active_toggl_hours). The enqueue
        # helper stuffs FIKEN_POLL_SINGLETON_KEY into gmail_thread_id so
        # cron + manual /debug/fiken/poll collapse to one run.
        Index(
            "uq_sync_tasks_active_fiken_poll",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text(
                "status IN ('pending','in_progress') AND task_type = 'fiken_poll'"
            ),
        ),
        # Drives the worker's claim query: filter by status, FIFO by oldest
        # next_attempt_at then id.
        Index("ix_sync_tasks_claim", "status", "next_attempt_at", "id"),
        # Per-project status rollup for the Projects-DB dot: "any active/failed
        # task for this project?".
        Index("ix_sync_tasks_project", "project_page_id", "status"),
    )
