"""Self-healing audit of a Frame.io version stack → Notion (Phase 2.5+).

This is the engine behind `frame_version_sync` after the audit refactor.
It replaces the original "flip status to Ferdig blindly" behavior with a
walk over the stack that:

  1. Identifies which child file IS our V00 placeholder — by **cached
     file id**, NOT by sort index. The old code (sync_frame_version
     pre-audit, `get_file_version_info`) assumed the placeholder was at
     index 0 in the stack sorted by `created_at`. That's only true when
     the placeholder was created FIRST. When a client drags a
     pre-existing Frame file (with an older `created_at`) onto our V00,
     the dragged file ends up at index 0 and our placeholder slides to
     index 1 → every comment on the real delivery gets classified as
     "V00 placeholder noise" and dropped.

  2. Treats every other child as a real version. The oldest non-
     placeholder file is Korreksjonsrunde 1, the next is round 2, etc.
     This matches the team's mental model: each version they uploaded
     gets its own review round.

  3. Backfills any comments missing from the FrameComment cache. The
     comment's file id picks which round it lands in; its replies nest
     under their parent's Korreksjon row. `completed_at != null` →
     Notion `Ferdig` checked immediately.

  4. Enqueues `leveranse_status_recheck` to let the existing rollup
     engine ([sync_leveranse_status.py](src/gb_automations/sync/sync_leveranse_status.py))
     compute the right status (`Under arbeid` / `Oppgaver ferdig`). For
     the "clean upload, zero comments" case the rollup engine has a
     branch that promotes to `Ferdig` directly — see its docstring.

Idempotency: `FrameComment` PK is the Frame comment UUID with
`ON CONFLICT DO NOTHING`, so re-runs (a `file.versioned` retry, or a
fallback re-enqueue from `sync_frame_comments`) silently no-op on the
comments we've already synced. The `FrameKorreksjonsrunde` cache means
the round row find-or-create is also a no-op on re-runs.

Self-heal:
  - 404/422 on the stack endpoint → not a stack (or deleted) → `skipped`.
  - No cached placeholder matching any stack child → not our stack → `skipped`.
  - Notion errors during the per-comment writes → bubble up as `failed`;
    the queue worker retries with backoff, and idempotency carries us
    through the partial write.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from gb_automations.clients import frame as frame_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import (
    FrameComment,
    FrameLeveranseFolder,
    FrameProjectFolder,
)
from gb_automations.sync.frame_project_labels import frame_project_label_for_page

logger = logging.getLogger(__name__)


@dataclass
class FrameStackAuditResult:
    """Outcome of one audit pass over a Frame version stack.

    `action` semantics mirror the old `sync_frame_version` dataclass so
    the queue worker dispatch (`_process_frame_version_sync` in
    `jobs/queue_worker.py`) doesn't need to change. `written` /
    `unchanged` / `skipped` / `skipped_manual` / `failed` are the values
    the worker recognizes."""

    stack_id: str
    leveranse_page_id: str | None = None
    project_page_id: str | None = None
    rounds_seen: int = 0
    comments_inserted: int = 0
    comments_already_cached: int = 0
    action: str = "skipped"  # written | unchanged | skipped | skipped_manual | failed
    note: str | None = None
    # The per-round Korreksjonsrunde page ids that the audit either created
    # or reused. Surfaced for logs / debug; not used to drive control flow.
    round_oppgave_ids: dict[int, str] = field(default_factory=dict)


def _is_4xx(err: Exception, codes: tuple[int, ...] = (404, 422)) -> bool:
    return (
        isinstance(err, frame_client.FrameAPIError)
        and getattr(err, "status_code", None) in codes
    )


async def audit_frame_stack(stack_id: str) -> FrameStackAuditResult:
    """Audit one Frame version stack and reconcile its comments into Notion.

    Single entry point for both `file.versioned` events (which always pass
    a stack id) and the comment-side fallback in `sync_frame_comments` (a
    round-0 misclassification re-enqueues the stack id here).
    """
    result = FrameStackAuditResult(stack_id=stack_id)

    if not settings.sync_frame:
        result.note = "SYNC_FRAME=false"
        return result

    # 1. Fetch stack children. 422/404 → not a stack (deleted, or the
    # caller passed a folder id by mistake); skip cleanly.
    try:
        children = await frame_client.list_version_stack_children(stack_id)
    except frame_client.FrameAPIError as err:
        if _is_4xx(err):
            logger.info(
                "frame_stack_audit: stack %s 4xx — not a stack or deleted; skipping",
                stack_id,
            )
            result.note = "stack 4xx"
            return result
        logger.exception(
            "frame_stack_audit: list_version_stack_children failed for %s", stack_id
        )
        result.action = "failed"
        result.note = f"Frame: {err}"
        return result
    except Exception as err:  # noqa: BLE001
        logger.exception("frame_stack_audit: list children crashed for %s", stack_id)
        result.action = "failed"
        result.note = str(err)
        return result

    if not children:
        logger.info("frame_stack_audit: stack %s has no children — skipping", stack_id)
        result.note = "stack empty"
        return result

    child_ids = [c.get("id") for c in children if c.get("id")]

    # 2. Match against our cache. The placeholder we created at Initialize
    # time has a stable file id; finding it among the stack's children
    # proves this stack belongs to a Leveranse we own.
    async with SessionLocal() as session:
        stmt = select(FrameLeveranseFolder).where(
            FrameLeveranseFolder.frame_placeholder_file_id.in_(child_ids)
        )
        leveranse_row = (await session.execute(stmt)).scalar_one_or_none()

    if leveranse_row is None:
        logger.info(
            "frame_stack_audit: stack %s has %d children but none match a cached "
            "placeholder — untracked stack, skipping",
            stack_id,
            len(children),
        )
        result.note = "untracked stack"
        return result

    leveranse_page_id = leveranse_row.notion_page_id
    project_page_id = leveranse_row.project_page_id
    placeholder_file_id = leveranse_row.frame_placeholder_file_id
    result.leveranse_page_id = leveranse_page_id
    result.project_page_id = project_page_id

    project_label = await frame_project_label_for_page(project_page_id)

    # 3. Partition: the placeholder is the file we identified by ID; every
    # other child is a real version. Sort the real versions ascending by
    # created_at so the OLDEST non-placeholder is round 1, the next round 2,
    # etc. This is the load-bearing semantic shift: rounds are assigned to
    # non-placeholder files in upload order, NOT to a child's index in the
    # full sorted stack.
    real_versions = [c for c in children if c.get("id") != placeholder_file_id]
    real_versions.sort(key=lambda c: c.get("created_at", ""))

    if not real_versions:
        # Only V00 present — no real delivery yet. file.versioned shouldn't
        # fire in this case, but defense-in-depth.
        logger.info(
            "frame_stack_audit: stack %s only contains the placeholder — "
            "no real delivery, skipping (project=%s)",
            stack_id,
            project_label,
        )
        result.note = "only V00 in stack"
        return result

    # Lazy imports: these modules import this module's sibling
    # sync_frame_comments which imports back. Defer until needed to avoid
    # the import cycle when the queue worker pulls us in.
    from gb_automations.sync.queue import enqueue_leveranse_status_recheck
    from gb_automations.sync.sync_frame_comments import (
        _ensure_korreksjonsrunde_oppgave,
        _persist_frame_comment,
        _resolve_commenter,
    )

    # 4. For each non-placeholder version, walk its comments. Comments
    # stay on the file they were posted on; the round number is the
    # version's index in the non-placeholder ordering.
    for round_idx, version in enumerate(real_versions, start=1):
        file_id = version.get("id")
        if not file_id:
            continue
        await _backfill_round(
            file_id=file_id,
            round_number=round_idx,
            leveranse_page_id=leveranse_page_id,
            project_page_id=project_page_id,
            project_label=project_label,
            stack_id=stack_id,
            result=result,
            ensure_round=_ensure_korreksjonsrunde_oppgave,
            resolve_commenter=_resolve_commenter,
            persist=_persist_frame_comment,
        )
        result.rounds_seen += 1

    # 5. Hand off to the rollup engine. It reads the active round's
    # Korreksjon children and writes the correct deliverable status —
    # `Under arbeid` / `Oppgaver ferdig` / `Ferdig` (the latter via the
    # clean-upload branch when no rounds exist but a real version does).
    try:
        await enqueue_leveranse_status_recheck(leveranse_page_id)
    except Exception:
        logger.exception(
            "frame_stack_audit: enqueue_leveranse_status_recheck failed for %s "
            "(non-fatal)",
            leveranse_page_id,
        )

    # `action=written` means we did something observable (at minimum we
    # enqueued the rollup; usually we also inserted comments). The worker
    # treats this as a successful task.
    result.action = "written"
    logger.info(
        "frame_stack_audit: stack %s (project=%s) leveranse %s → %d round(s), "
        "%d comment(s) inserted, %d already cached",
        stack_id,
        project_label,
        leveranse_page_id,
        result.rounds_seen,
        result.comments_inserted,
        result.comments_already_cached,
    )
    return result


async def _backfill_round(
    *,
    file_id: str,
    round_number: int,
    leveranse_page_id: str,
    project_page_id: str | None,
    project_label: str,
    stack_id: str,
    result: FrameStackAuditResult,
    ensure_round,
    resolve_commenter,
    persist,
) -> None:
    """Walk every comment on `file_id`, creating Korreksjon rows for any
    that aren't already in the FrameComment cache.

    The round row (Korreksjonsrunde N sub-row under the deliverable) is
    created lazily on the first comment of this round — same find-or-create
    behavior as the live `sync_frame_comment` path. We pass the helper in
    rather than importing it at module load to keep the cycle clean.

    Comments and their replies arrive in the same `list_comments` call
    (`?include=replies`). We flatten and order them so a parent is always
    processed before its replies — replies look up their parent's cached
    `oppgave_page_id` to set the Notion Parent-item relation, and that
    cache row is only there once the parent has been persisted.
    """
    try:
        comments = await frame_client.list_comments(file_id)
    except frame_client.FrameAPIError as err:
        if _is_4xx(err):
            logger.info(
                "frame_stack_audit: list_comments 4xx on file %s (project=%s) — "
                "file gone or no access, skipping this round",
                file_id,
                project_label,
            )
            return
        logger.exception(
            "frame_stack_audit: list_comments failed on file %s (project=%s)",
            file_id,
            project_label,
        )
        raise

    if not comments:
        return

    # Round row gets created on demand only if we have at least one
    # comment to anchor against it — saves a Notion write for files with
    # no review activity.
    runde_oppgave_id: str | None = None

    # Flatten top-levels + replies into one pass. For each top-level
    # comment we also enqueue any nested replies right after it, so the
    # FrameComment cache for the parent lands before the reply tries to
    # read it.
    for parent in comments:
        # The parent itself (a top-level comment on this file).
        runde_oppgave_id = await _process_one_comment(
            comment=parent,
            parent_comment_id=None,
            file_id=file_id,
            round_number=round_number,
            leveranse_page_id=leveranse_page_id,
            project_page_id=project_page_id,
            project_label=project_label,
            runde_oppgave_id=runde_oppgave_id,
            ensure_round=ensure_round,
            resolve_commenter=resolve_commenter,
            persist=persist,
            result=result,
        )

        # Replies (Frame V4 surfaces them nested on the parent payload).
        for reply in parent.get("replies") or []:
            runde_oppgave_id = await _process_one_comment(
                comment=reply,
                parent_comment_id=parent.get("id"),
                file_id=file_id,
                round_number=round_number,
                leveranse_page_id=leveranse_page_id,
                project_page_id=project_page_id,
                project_label=project_label,
                runde_oppgave_id=runde_oppgave_id,
                ensure_round=ensure_round,
                resolve_commenter=resolve_commenter,
                persist=persist,
                result=result,
            )

    if runde_oppgave_id is not None:
        result.round_oppgave_ids[round_number] = runde_oppgave_id


async def _process_one_comment(
    *,
    comment: dict,
    parent_comment_id: str | None,
    file_id: str,
    round_number: int,
    leveranse_page_id: str,
    project_page_id: str | None,
    project_label: str,
    runde_oppgave_id: str | None,
    ensure_round,
    resolve_commenter,
    persist,
    result: FrameStackAuditResult,
) -> str | None:
    """Backfill one Frame comment (top-level OR reply). Returns the
    `runde_oppgave_id` (which may have just been created on this call so
    subsequent comments of the same round can reuse it).

    Already-cached comments (PK conflict on `frame_comments`) are
    counted and skipped — re-runs and overlapping triggers (a
    file.versioned plus a fallback enqueue from sync_frame_comments)
    are silently idempotent.
    """
    comment_id = comment.get("id")
    if not comment_id:
        return runde_oppgave_id

    async with SessionLocal() as session:
        if await session.get(FrameComment, comment_id) is not None:
            result.comments_already_cached += 1
            return runde_oppgave_id

    # First comment of this round → create the Korreksjonsrunde N row.
    if runde_oppgave_id is None:
        runde_oppgave_id = await ensure_round(
            leveranse_page_id=leveranse_page_id,
            round_number=round_number,
            project_page_id=project_page_id,
        )
        if runde_oppgave_id is None:
            # OPPGAVER_DB_ID unset, or create failed. Skip this round
            # entirely — the FrameComment cache is NOT updated, so a
            # later retry can pick it up once the env is set.
            logger.warning(
                "frame_stack_audit: cannot create Korreksjonsrunde %d for "
                "deliverable %s (project=%s) — skipping comment %s",
                round_number,
                leveranse_page_id,
                project_label,
                comment_id,
            )
            return None

    text = (comment.get("text") or "").strip() or "(empty comment)"
    body_snippet = (comment.get("text") or "")[:512]
    commenter_contact_id = await resolve_commenter(comment)

    # Replies nest under the parent comment's Korreksjon (Parent item,
    # no round relation). Top-levels relate directly to the round.
    parent_korreksjon_id: str | None = None
    if parent_comment_id is not None:
        async with SessionLocal() as session:
            parent_cache = await session.get(FrameComment, parent_comment_id)
        if parent_cache is not None and parent_cache.oppgave_page_id:
            parent_korreksjon_id = parent_cache.oppgave_page_id
        else:
            # Parent didn't land in the cache for some reason (race, or a
            # legacy row from the old bullet model). Fall back to a
            # top-level Korreksjon under the round — the reply still
            # lands; the visual nesting is one level shallower.
            logger.warning(
                "frame_stack_audit: reply %s parent %s has no per-comment "
                "Korreksjon — attaching at round %d top-level instead",
                comment_id,
                parent_comment_id,
                round_number,
            )

    try:
        created = await notion_client.create_korreksjon_row(
            name=text,
            korreksjonsrunde_page_id=runde_oppgave_id,
            round_number=round_number,
            project_page_id=project_page_id,
            parent_korreksjon_id=parent_korreksjon_id,
            commenter_contact_id=commenter_contact_id,
            commented_at=comment.get("created_at"),
        )
    except Exception:
        logger.exception(
            "frame_stack_audit: create_korreksjon_row failed for comment %s "
            "(project=%s) — bubbling up so the queue retries",
            comment_id,
            project_label,
        )
        raise

    oppgave_page_id = created.get("id")

    # Mirror Frame's completed_at into Notion's Ferdig checkbox right
    # away — saves a round-trip through the propagation engine for
    # comments that arrived already-resolved (the common case for the
    # backfill: legacy comments on a pre-existing file are often
    # already done in Frame).
    if comment.get("completed_at") is not None and oppgave_page_id:
        try:
            await notion_client.set_row_done(oppgave_page_id, True)
        except Exception:
            logger.exception(
                "frame_stack_audit: initial set_row_done failed for %s — "
                "non-fatal, propagation engine will reconcile later",
                oppgave_page_id,
            )

    async with SessionLocal() as session:
        await persist(
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

    result.comments_inserted += 1
    logger.info(
        "frame_stack_audit: backfilled %s comment %s by %r → leveranse %s "
        "round %d → oppgave %s (project=%s, completed=%s)",
        "reply to " + (parent_comment_id or "") if parent_comment_id else "top-level",
        comment_id,
        _author_display(comment),
        leveranse_page_id,
        round_number,
        oppgave_page_id,
        project_label,
        comment.get("completed_at") is not None,
    )
    return runde_oppgave_id


def _author_display(comment: dict) -> str:
    """Mirror `sync_frame_comments._author_display` for log narration —
    external/guest commenters have no `owner` (Frame privacy policy)."""
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


__all__ = ["FrameStackAuditResult", "audit_frame_stack"]
