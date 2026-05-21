from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
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


class AttachmentFingerprint(Base):
    """Tracks (sender, content-hash) repetition counts for signature detection.

    The signal: an image bytes-hash that has appeared 2+ times from the same
    sender is almost certainly a signature decoration (company logo, etc.).
    Real attachments are unique-per-email; signatures repeat.

    On each attachment we compute sha1(content), look up the row, and skip
    upload to Drive if `seen_count >= 2`. First sighting always uploads.

    Content hash makes this robust to Gmail's per-email auto-numbering of
    inline image filenames (`image001.png` in one email might be a totally
    different image than `image001.png` in the next).
    """

    __tablename__ = "attachment_fingerprints"

    sender_email: Mapped[str] = mapped_column(String(254), primary_key=True)
    content_sha1: Mapped[str] = mapped_column(String(40), primary_key=True)
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # First filename we saw this image under — purely for debugging when
    # browsing the table. Names may vary across emails ("image001.png" vs
    # "image004.png") even when the bytes are identical, so this is hint-only.
    first_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ThreadAttachment(Base):
    """Content hashes already uploaded to Drive for a thread — durable across syncs.

    `ThreadAttachmentTracker` dedups attachments within a single sync, but a new
    reply re-carries the whole quoted MIME tree, so without a persisted record
    every reply re-uploads the same bytes to Drive under the new replier's name.
    Keyed by (thread, content_sha1): identical bytes are the same image no matter
    which message now carries them, so an exact-hash hit anywhere in the thread
    means "already on Drive, skip". Seeded into the per-sync tracker at sync start
    and written back on every successful upload.

    `drive_links` carries the Drive `{name, url}` entries the bytes uploaded to
    (one per matched project subfolder). It exists so a re-sync — which skips the
    *upload* because the sha1 is already known — can still set the Notion row's
    Files property: "already on Drive" must not mean "can't re-link". Without it
    the upload-dedup silently doubled as a link-suppressor, leaving rows file-less
    while the bytes sat in Drive.
    """

    __tablename__ = "thread_attachments"

    gmail_thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_sha1: Mapped[str] = mapped_column(String(40), primary_key=True)
    # First filename we saw these bytes under — debugging hint only; Gmail
    # renumbers inline image names across messages even for identical bytes.
    first_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # List of {"name", "url"} dicts — the Drive links this content uploaded to,
    # one per matched project subfolder. Nullable for rows written before the
    # column existed; treated as "no known links" when absent.
    drive_links: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
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
    # `WHERE user_email = ?` — keep that hot path on a real index.
    __table_args__ = (
        Index("ix_project_labels_user_email", "user_email"),
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


# Status values for SyncTask. Kept as a plain string + CHECK (not a Postgres
# ENUM) because ENUMs are painful to extend via Alembic and the rest of the
# schema uses plain strings/booleans.
SYNC_TASK_STATUSES = ("pending", "in_progress", "done", "failed")


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
    user_email: Mapped[str] = mapped_column(String(254), nullable=False)
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
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
        # At most one ACTIVE (pending/in_progress) row per thread — this is the
        # dedup that makes re-enqueue on every push/reply a safe no-op. The
        # partial predicate still allows a new row once the prior one is
        # done/failed (so a later reply re-enqueues the thread).
        Index(
            "uq_sync_tasks_active_thread",
            "user_email",
            "gmail_thread_id",
            unique=True,
            postgresql_where=text("status IN ('pending','in_progress')"),
        ),
        # Drives the worker's claim query: filter by status, FIFO by oldest
        # next_attempt_at then id.
        Index("ix_sync_tasks_claim", "status", "next_attempt_at", "id"),
        # Per-project status rollup for the Projects-DB dot: "any active/failed
        # task for this project?".
        Index("ix_sync_tasks_project", "project_page_id", "status"),
    )
