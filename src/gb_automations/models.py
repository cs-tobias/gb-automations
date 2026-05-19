from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, Integer, String, func
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
