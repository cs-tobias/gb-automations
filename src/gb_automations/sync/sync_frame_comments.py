"""Frame.io comment → Notion Korreksjonsrunde Oppgave (Phase 2).

When a Frame V4 webhook fires `comment.created` (or `.updated` / `.completed`)
we enqueue a `frame_comment_sync` task carrying the comment id. The queue
worker drains it into `sync_frame_comment(comment_id)`, which:

  1. Fetches the comment from Frame with `?include=owner,replies`.
  2. Joins comment.file_id → FrameLeveranseFolder.frame_placeholder_file_id
     → the Notion Leveranse page.
  3. Reads the file's version stack to derive the round number
     (V01 = round 1, V02 = round 2; V00 = round 0 / pre-delivery).
  4. Decides "top-level vs reply" by checking whether this comment id
     appears in any sibling comment's `replies: [...]` array — Frame V4
     does NOT expose `parent_id` on the reply object itself.
  5. Top-level: find-or-create the "Korreksjonsrunde N" Oppgave row,
     append the comment as a bullet, persist FrameComment with the
     bullet's `notion_block_id` (for later replies to indent under).
  6. Reply: load the parent's FrameComment row, PATCH its
     `notion_block_id` with a nested-bullet child.
  7. INSERT INTO frame_comments ON CONFLICT DO NOTHING — engine-level
     idempotency. A webhook redelivery for the same comment id is a
     no-op (the active-dedup index on sync_tasks would also collapse a
     same-time double-fire, but defense-in-depth is cheap).

Verified V4 shapes (against Goldbox's workspace, 2026-05-26):
    GET /accounts/{aid}/comments/{cid}?include=owner,replies
    {
      "data": {
        "id": "<uuid>", "text": "...", "file_id": "<uuid>",
        "created_at": "ISO", "updated_at": "ISO",
        "completed_at": null | "ISO",
        "owner": null | {"id","name","email","active",...},
        "replies": [
          {"id": "<uuid>", "text": "...", "file_id": "<same>",
           "created_at": "ISO", ...}  # NB: no `parent_id` on reply
        ],
        "annotation": null | "[{...pen sketch...}]",
        ...
      }
    }

owner is null for external/guest commenters (Frame privacy policy). The
engine degrades to "External reviewer" in that case.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import (
    OPPGAVE_KIND_KORREKSJON,
    OPPGAVE_KIND_KORREKSJONSRUNDE,
    STATUS_KLAR_TIL_OPPSTART,
    settings,
)
from gb_automations.db import SessionLocal
from gb_automations.models import FrameComment, FrameLeveranseFolder

logger = logging.getLogger(__name__)


@dataclass
class FrameCommentResult:
    comment_id: str
    project_page_id: str | None = None
    leveranse_page_id: str | None = None
    oppgave_page_id: str | None = None
    round_number: int | None = None
    is_reply: bool = False
    parent_comment_id: str | None = None
    notion_block_id: str | None = None
    action: str = "skipped"  # created | appended | reply | unchanged | skipped | failed
    note: str | None = None


# ============================================================
# Self-heal helpers (mirror sync_frame's 404 → evict pattern)
# ============================================================


def _is_404(err: Exception) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) == 404
    )


def _author_display(comment: dict) -> str:
    """Render the author segment for the Notion bullet.

    `owner` is null for external/guest commenters by Frame's privacy
    policy — degrade to "External reviewer" rather than dropping the
    attribution slot entirely (the team needs *something* to scan)."""
    owner = comment.get("owner")
    if not isinstance(owner, dict):
        return "External reviewer"
    name = owner.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    email = owner.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return "External reviewer"


def _bullet_text(comment: dict) -> str:
    """`<Author>: <comment text>` — same format used for both top-level
    and reply bullets. Empty body falls back to a placeholder so the
    bullet still has *something* to display (Notion rejects empty
    rich_text in some block types)."""
    author = _author_display(comment)
    text = (comment.get("text") or "").strip()
    if not text:
        text = "(empty comment)"
    return f"{author}: {text}"


# ============================================================
# Parent lookup (Frame doesn't expose comment.parent_id, so we look)
# ============================================================


async def _find_parent_comment_id(file_id: str, comment_id: str) -> str | None:
    """If `comment_id` is a reply, return the id of its parent comment.

    Frame V4's reply object does NOT carry a `parent_id` of its own — the
    only way to tell a reply from a top-level comment is to list the
    file's top-level comments (with `?include=replies`) and check whether
    `comment_id` appears inside any of their `replies: [...]` arrays.

    The list is bounded by "comments on this one file" so the cost stays
    sane. If `comment_id` is itself a top-level comment, every
    `replies` array we walk will miss it → return None.
    """
    siblings = await frame_client.list_comments(file_id)
    for sibling in siblings:
        if sibling.get("id") == comment_id:
            # Found ourselves in the top-level list → we're a top-level
            # comment, not a reply.
            return None
        for reply in sibling.get("replies") or []:
            if reply.get("id") == comment_id:
                return sibling.get("id")
    # Not found anywhere — comment was probably deleted between the
    # webhook firing and our list call. Treat as top-level (the engine
    # then probably 404s on the get_comment call and self-heals).
    return None


# ============================================================
# Resolve comment → Leveranse + round
# ============================================================


async def _resolve_leveranse(
    session: AsyncSession, file_id: str
) -> FrameLeveranseFolder | None:
    """Look up the FrameLeveranseFolder owning this comment's file.

    Two cases, in order:

      (a) Direct match — the comment is on our cached placeholder file
          (the V00 case, before any real delivery). Fast path.

      (b) Version-stack match — Frame creates a new File id for every
          version uploaded. When the team drags V01 on top of the
          placeholder, Frame wraps both V00 and V01 inside a new
          version_stack entity; the new V01 has its own id we've never
          seen. To resolve: look at the comment's file's `parent_id`. If
          that's a version stack, list the stack's children and find one
          of our cached placeholders sitting alongside the new version.
          That cached placeholder identifies the Leveranse.

    Returns None when the comment lands on a Frame file with no link to
    any tracked Leveranse — someone uploaded a stray file directly in
    Frame, or commented on a folder we don't manage. The engine logs +
    marks `skipped` in that case.
    """
    # (a) Direct match — fast path for V00 comments (the placeholder
    # before any real delivery wraps it in a stack).
    stmt = select(FrameLeveranseFolder).where(
        FrameLeveranseFolder.frame_placeholder_file_id == file_id
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row

    # (b) Version-stack match. Fetch the file to learn its parent_id.
    try:
        file_obj = await frame_client.get_file(file_id)
    except frame_client.FrameAPIError as err:
        if _is_404(err):
            return None
        raise
    parent_id = file_obj.get("parent_id")
    if not parent_id:
        return None

    # Parent is either the task folder (the V00 case — would've matched
    # in (a)) or the version_stack id. Try the version_stack endpoint;
    # if it 422s the parent is a folder and there's nothing more we can
    # match against.
    try:
        siblings = await frame_client.list_version_stack_children(parent_id)
    except frame_client.FrameAPIError as err:
        if err.status_code in (404, 422):
            return None
        raise
    sibling_ids = [s.get("id") for s in siblings if s.get("id")]
    if not sibling_ids:
        return None
    stmt = select(FrameLeveranseFolder).where(
        FrameLeveranseFolder.frame_placeholder_file_id.in_(sibling_ids)
    )
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is not None:
        logger.info(
            "frame_comments: comment file %s is in version stack %s, "
            "matched leveranse %s via cached placeholder",
            file_id,
            parent_id,
            row.notion_page_id,
        )
    return row


# ============================================================
# Find-or-create the Korreksjonsrunde Oppgave row
# ============================================================


async def _ensure_korreksjonsrunde_oppgave(
    *, leveranse_page_id: str, round_number: int
) -> str | None:
    """Return the page id of the "Korreksjonsrunde N" Oppgave for this
    Leveranse, creating it lazily on the first comment of the round.

    Returns None if OPPGAVER_DB_ID is unset OR the create fails — caller
    treats that as "skip the Notion write" (the FrameComment row is also
    NOT persisted, so a retry can succeed once the env var is set).
    """
    if not settings.oppgaver_db_id:
        logger.info(
            "frame_comments: OPPGAVER_DB_ID unset — skipping korreksjonsrunde "
            "create for leveranse %s round %d",
            leveranse_page_id,
            round_number,
        )
        return None
    existing = await notion_client.find_oppgave_by_round(
        leveranse_page_id, round_number=round_number
    )
    if existing:
        return existing
    name = f"Korreksjonsrunde {round_number}"
    created = await notion_client.create_oppgave_row(
        name=name,
        leveranse_page_id=leveranse_page_id,
        kind=OPPGAVE_KIND_KORREKSJONSRUNDE,
        round_number=round_number,
    )
    page_id = created.get("id")
    logger.info(
        "frame_comments: created Oppgave %r for leveranse %s round %d (page %s)",
        name,
        leveranse_page_id,
        round_number,
        page_id,
    )
    return page_id


# ============================================================
# Persistence (engine-level idempotency)
# ============================================================


async def _persist_frame_comment(
    session: AsyncSession,
    *,
    frame_comment_id: str,
    frame_file_id: str,
    leveranse_page_id: str,
    oppgave_page_id: str,
    round_number: int,
    parent_comment_id: str | None,
    notion_block_id: str | None,
    body_snippet: str,
) -> None:
    """INSERT … ON CONFLICT DO NOTHING into frame_comments.

    The Notion writes have already succeeded by this point (Decision 3 in
    the Phase 2 plan: persist AFTER the bullet lands so a Notion outage
    on the append yields a queue retry, not a silently-deduped
    re-attempt that loses the bullet). The very rare case where Notion
    succeeded but the row insert fails → next webhook redelivery
    creates a duplicate bullet; acceptable rare cost vs always-lose-the-
    bullet on the other ordering.
    """
    stmt = pg_insert(FrameComment).values(
        frame_comment_id=frame_comment_id,
        frame_file_id=frame_file_id,
        leveranse_page_id=leveranse_page_id,
        oppgave_page_id=oppgave_page_id,
        round_number=round_number,
        parent_comment_id=parent_comment_id,
        notion_block_id=notion_block_id,
        body_snippet=body_snippet[:512],
    ).on_conflict_do_nothing(index_elements=["frame_comment_id"])
    await session.execute(stmt)
    await session.commit()


# ============================================================
# The engine
# ============================================================


async def sync_frame_comment(comment_id: str) -> FrameCommentResult:
    """Process one Frame comment id end-to-end.

    Two-path engine after Phase 2.5:

      Path A — comment NOT yet in the FrameComment cache (`comment.created`):
        Fetch the comment from Frame, resolve Leveranse + round, create a
        per-comment Korreksjon sub-Oppgave under the Korreksjonsrunde N row
        (creating the round row itself lazily on first comment). On reply
        events the new Oppgave is a sub-row of its parent comment's Oppgave
        (3-level deep). Persist FrameComment with the new oppgave_page_id.
        Flip Leveranse status to `Klar til oppstart`.

      Path B — comment IS already in the cache
      (`comment.completed` / `comment.uncompleted` for an existing row):
        Propagate the new `completed_at` state to the linked Oppgave's
        Ferdig checkbox via the read-first idempotent
        `notion_client.set_oppgave_done`. Then enqueue a
        `leveranse_status_recheck` so the rollup runs.

    Idempotent in both paths. Loop-prevention happens at the Notion
    helper level (read-first / skip-if-same).
    """
    result = FrameCommentResult(comment_id=comment_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result

    # Fetch comment first — we need the current completed_at to decide
    # which path to take.
    try:
        comment = await frame_client.get_comment(comment_id)
    except frame_client.FrameAPIError as err:
        if _is_404(err):
            logger.info(
                "frame_comments: comment %s 404 — deleted in Frame; marking done",
                comment_id,
            )
            result.action = "skipped"
            result.note = "comment deleted in Frame (404)"
            return result
        logger.exception("frame_comments: fetch failed for %s", comment_id)
        result.action = "failed"
        result.note = f"frame.get_comment 5xx: {err}"
        return result
    except Exception as err:  # noqa: BLE001
        logger.exception("frame_comments: fetch crashed for %s", comment_id)
        result.action = "failed"
        result.note = str(err)
        return result

    # Path B — completion-state propagation for a cached comment. The
    # comment.created path below also writes a FrameComment cache row,
    # so any 'previously seen' comment id ends up here. Without this
    # early branch, the engine would short-circuit on completion events
    # and never propagate the new state.
    async with SessionLocal() as session:
        cached = await session.get(FrameComment, comment_id)

    if cached is not None:
        return await _propagate_completion(comment_id, comment, cached, result)

    # Path A — brand-new comment. Continue with the create flow.
    file_id = comment.get("file_id")
    if not file_id:
        logger.warning(
            "frame_comments: comment %s has no file_id — skipping (shape %s)",
            comment_id,
            sorted(comment.keys()),
        )
        result.note = "comment payload missing file_id"
        return result

    async with SessionLocal() as session:
        leveranse_row = await _resolve_leveranse(session, file_id)
    if leveranse_row is None:
        logger.info(
            "frame_comments: comment %s on file %s is not on a tracked "
            "Leveranse placeholder — skipping",
            comment_id,
            file_id,
        )
        result.action = "skipped"
        result.note = "comment on untracked file"
        return result

    leveranse_page_id = leveranse_row.notion_page_id
    project_page_id = leveranse_row.project_page_id
    result.leveranse_page_id = leveranse_page_id
    result.project_page_id = project_page_id

    try:
        round_number, _stack_id = await frame_client.get_file_version_info(file_id)
    except Exception:
        logger.exception(
            "frame_comments: version derivation failed for file %s — "
            "assuming round 0",
            file_id,
        )
        round_number = 0
    result.round_number = round_number

    # V00 placeholder noise — see Phase 2 decision.
    if round_number == 0:
        logger.info(
            "frame_comments: comment %s on V00 placeholder (file %s) — "
            "dropping (placeholder noise, not a real delivery yet)",
            comment_id,
            file_id,
        )
        result.action = "skipped"
        result.note = "comment on V00 placeholder (pre-delivery noise)"
        return result

    # Reply detection (Phase 2 logic, unchanged): walk the file's
    # top-level comments looking for our id in any of their `replies`.
    parent_comment_id = await _find_parent_comment_id(file_id, comment_id)
    result.is_reply = parent_comment_id is not None
    result.parent_comment_id = parent_comment_id

    name = _bullet_text(comment)  # used as the row title; same author + text format
    body_snippet = (comment.get("text") or "")[:512]

    # Find or create the Korreksjonsrunde N row. Round-create is rare —
    # only the first comment of a new round needs it.
    runde_oppgave_id = await _ensure_korreksjonsrunde_oppgave(
        leveranse_page_id=leveranse_page_id,
        round_number=round_number,
    )
    if runde_oppgave_id is None:
        result.action = "skipped"
        result.note = "OPPGAVER_DB_ID unset or create failed"
        return result

    # Determine the parent for the new sub-row:
    #   - top-level comment → child of the Korreksjonsrunde row.
    #   - reply             → child of the parent Korreksjon row (3-deep).
    if parent_comment_id is None:
        parent_oppgave_id = runde_oppgave_id
    else:
        async with SessionLocal() as session:
            parent_cache = await session.get(FrameComment, parent_comment_id)
        if parent_cache is None or not parent_cache.oppgave_page_id:
            # Parent webhook hasn't been processed yet OR parent is a
            # legacy Phase 2 bullet (oppgave_page_id pointed at the round
            # row, not a per-comment row). Fall back to parenting under
            # the round — the reply still lands; nesting is just one level
            # shallower than ideal.
            logger.warning(
                "frame_comments: reply %s — parent %s has no per-comment "
                "Oppgave; parenting under Korreksjonsrunde %d instead",
                comment_id,
                parent_comment_id,
                round_number,
            )
            parent_oppgave_id = runde_oppgave_id
        else:
            parent_oppgave_id = parent_cache.oppgave_page_id

    # Create the per-comment Oppgave row.
    try:
        created = await notion_client.create_oppgave_row(
            name=name,
            leveranse_page_id=leveranse_page_id,
            kind=OPPGAVE_KIND_KORREKSJON,
            round_number=round_number,
            parent_oppgave_id=parent_oppgave_id,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_comments: failed to create Korreksjon Oppgave for "
            "comment %s — queue will retry",
            comment_id,
        )
        result.action = "failed"
        result.note = f"Notion create: {err}"
        return result

    oppgave_page_id = created.get("id")
    result.oppgave_page_id = oppgave_page_id
    result.action = "reply" if parent_comment_id else "created"

    # If the comment is already resolved in Frame at create time
    # (e.g. an old comment we're just now syncing), mirror that into
    # the new Notion checkbox immediately.
    if comment.get("completed_at") is not None:
        try:
            await notion_client.set_oppgave_done(oppgave_page_id, True)
        except Exception:
            logger.exception(
                "frame_comments: initial set_oppgave_done failed for %s "
                "(non-fatal)",
                oppgave_page_id,
            )

    # Persist FrameComment last (Decision 3 from the original plan).
    # notion_block_id stays null in the sub-row model — replies are
    # separate rows, no per-bullet block to track.
    async with SessionLocal() as session:
        await _persist_frame_comment(
            session,
            frame_comment_id=comment_id,
            frame_file_id=file_id,
            leveranse_page_id=leveranse_page_id,
            oppgave_page_id=oppgave_page_id,
            round_number=round_number,
            parent_comment_id=parent_comment_id,
            notion_block_id=None,
            body_snippet=body_snippet,
        )

    # First comment of a new round → flip Leveranse status to
    # "Klar til oppstart". Only fires for TOP-LEVEL comments (replies
    # arrive after the first top-level, so the status flip already
    # happened). set_leveranse_status is read-first/idempotent: if the
    # team has already moved past Klar til oppstart (e.g. they ticked
    # a checkbox before the next webhook delivery), this is a no-op.
    if parent_comment_id is None:
        try:
            await notion_client.set_leveranse_status(
                leveranse_page_id, STATUS_KLAR_TIL_OPPSTART
            )
        except Exception:
            logger.exception(
                "frame_comments: set_leveranse_status(Klar til oppstart) "
                "failed for %s (non-fatal)",
                leveranse_page_id,
            )

    logger.info(
        "frame_comments: %s comment %s by %r → leveranse %s round %d → oppgave %s",
        "reply to " + parent_comment_id if parent_comment_id else "top-level",
        comment_id,
        _author_display(comment),
        leveranse_page_id,
        round_number,
        oppgave_page_id,
    )
    return result


async def _propagate_completion(
    comment_id: str,
    comment: dict,
    cached: FrameComment,
    result: FrameCommentResult,
) -> FrameCommentResult:
    """Path B: propagate Frame's completed_at state to the cached
    comment's Oppgave Ferdig checkbox + enqueue a status recheck.

    Loop-prevention rests on notion_client.set_oppgave_done's
    read-first/skip-if-same behavior — when the propagation cycle
    bounces back (Notion writes → automation fires → engine reads
    Frame again), this branch sees Notion already matches and the
    helper returns "unchanged" without PATCHing.
    """
    # Lazy import to avoid a circular dep — queue.py imports the engine
    # for testing; we'd loop on module import otherwise.
    from gb_automations.sync.queue import enqueue_leveranse_status_recheck

    result.leveranse_page_id = cached.leveranse_page_id
    result.oppgave_page_id = cached.oppgave_page_id
    result.round_number = cached.round_number
    result.parent_comment_id = cached.parent_comment_id
    result.is_reply = cached.parent_comment_id is not None

    if not cached.oppgave_page_id:
        # Legacy Phase 2 cache rows pointed oppgave_page_id at the
        # round Oppgave, not a per-comment row. There's no individual
        # checkbox to flip for those — they're bullet-list artifacts.
        # No-op: the row's still here, status rollup just can't include
        # it. The team will see the Frame UI mark it resolved; the
        # Notion bullets stay as-is.
        result.action = "unchanged"
        result.note = "legacy bullet row — no per-comment checkbox to mirror"
        return result

    desired_done = comment.get("completed_at") is not None
    try:
        action = await notion_client.set_oppgave_done(
            cached.oppgave_page_id, desired_done
        )
    except Exception as err:  # noqa: BLE001
        logger.exception(
            "frame_comments: set_oppgave_done failed for %s — queue will retry",
            cached.oppgave_page_id,
        )
        result.action = "failed"
        result.note = f"Notion set_oppgave_done: {err}"
        return result

    result.action = "unchanged" if action == "unchanged" else "completed"
    result.note = (
        f"completion propagated: Frame={desired_done} Notion action={action}"
    )

    # Enqueue rollup recheck — even if set_oppgave_done was a no-op the
    # Leveranse's status may still need to move (e.g. a stale recheck
    # task missed an earlier toggle). The dedup index collapses rapid
    # bursts to one task.
    try:
        await enqueue_leveranse_status_recheck(cached.leveranse_page_id)
    except Exception:
        logger.exception(
            "frame_comments: enqueue_leveranse_status_recheck failed for %s "
            "(non-fatal)",
            cached.leveranse_page_id,
        )

    logger.info(
        "frame_comments: propagated completed=%s for comment %s → oppgave %s (notion=%s)",
        desired_done,
        comment_id,
        cached.oppgave_page_id,
        action,
    )
    return result
