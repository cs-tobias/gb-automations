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


class EmailContentDedup(Base):
    """(project, from_email, body_hash) → notion_page_id of the canonical row.

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

    PK is `(project_page_id, from_email, body_hash)` — that IS the lookup key,
    so no separate index needed. body_hash is SHA-256 hex of the SAME cleaned
    body string we write to Notion's `Melding`; from_email is lower-cased and
    may be empty for LLM-extracted name-only senders (an empty-empty match
    across two rows still legitimately means "same body, same project, same
    nameless sender" — we collapse those too).
    """

    __tablename__ = "email_content_dedup"

    project_page_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    from_email: Mapped[str] = mapped_column(String(254), primary_key=True)
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
    """Notion Leveranse page ↔ Frame.io task folder + its placeholder file.

    Phase 2 reframed what this table represents: a row in the (formerly
    "Oppgaver", now "Leveranser") Notion DB is a deliverable *entity* — one
    image / render — not a task. Each row provisions its own Frame folder
    under the project's discipline subfolder, plus a V00 placeholder file
    that becomes the base of Frame's version stack (V01 = first real
    delivery, V02 = revision after round 1, etc.).

    The placeholder file id (`frame_placeholder_file_id`) is what makes
    "first delivery uploads as V01 on top" work in Frame's UI; we persist
    it so:
    (a) Phase 2 comment polling joins comment.file_id → this row →
        Notion Leveranse page, and
    (b) a Leveranse rename preserves the placeholder rather than
        recreating it (and losing the version stack).

    `project_page_id` denormalized for "all Frame folders for project X"
    lookups (e.g. when a project's folder id is re-resolved after
    self-heal). Column names are kept as-is across the
    frame_task_folders → frame_leveranse_folders table rename — the
    semantic shift is "what does a row mean", not "what does each column
    hold".
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


# Temporary backward-compat alias to keep imports working between Step 2
# (this rename) and Step 5 (the rename pass through sync_frame.py + tests).
# Remove at the end of Step 5 — every call site should reference
# FrameLeveranseFolder by then.
FrameTaskFolder = FrameLeveranseFolder


class FrameComment(Base):
    """Frame.io comment ↔ Notion bullet block on a Korreksjonsrunde Oppgave.

    One row per Frame comment we've successfully synced. The primary key is
    the Frame comment UUID; INSERTs use ON CONFLICT DO NOTHING for engine-
    level idempotency (a re-delivered webhook for the same comment id is
    a no-op).

    `notion_block_id` is the id of the bullet we wrote to the
    Korreksjonsrunde Oppgave page. Replies look this up to PATCH a nested
    child bullet under their parent block (Decision 2 in the Phase 2
    plan), avoiding the cost of rebuilding the whole bullet section.

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
# on comment.created / comment.updated, the receiver enqueues one of
# these per comment id, and the worker fetches the full comment and
# writes a bullet into the corresponding Korreksjonsrunde Oppgave row.
# New types go here + the CHECK below + a branch in jobs/queue_worker._process.
SYNC_TASK_TYPES = (
    "thread",
    "label_sync",
    "nas_folder_sync",
    "task_folder_sync",
    "frame_project_sync",
    "frame_leveranse_sync",
    "frame_comment_sync",
)


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
            "'frame_comment_sync')",
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
        # Drives the worker's claim query: filter by status, FIFO by oldest
        # next_attempt_at then id.
        Index("ix_sync_tasks_claim", "status", "next_attempt_at", "id"),
        # Per-project status rollup for the Projects-DB dot: "any active/failed
        # task for this project?".
        Index("ix_sync_tasks_project", "project_page_id", "status"),
    )
