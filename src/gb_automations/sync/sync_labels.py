"""Project → Gmail label sync engine.

Reconcile + create a project's Gmail label across every active mailbox, plus the
NAS folder for the project. This is the work the Projects-DB "Sync to Gmail"
button used to do INLINE in the webhook — which intermittently exceeded Notion's
button-webhook timeout and showed "failed to execute". It now runs in the durable
queue worker (a `label_sync` task), so the button just enqueues and returns. See
docs/misc/gotchas.md and CLAUDE.md "sync work is queued, not inline".

Idempotent and self-healing, like sync_thread: re-running fixes drift (Notion
rename, Gmail-side rename, label deleted) and creates anything missing.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any

from googleapiclient.errors import HttpError
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import nas as nas_client
from gb_automations.clients import notion as notion_client
from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import ProjectFolder, ProjectLabel, User
from gb_automations.utils.labels import project_label_path

logger = logging.getLogger(__name__)

# Gmail client is sync; offload to a thread pool to keep the event loop free.
# Running in the single queue worker (CONCURRENCY=1) means only one label_sync
# task uses this at a time, so the pool fans out the per-user calls WITHIN a
# task without ever bursting Gmail across tasks.
_executor = ThreadPoolExecutor(max_workers=4)


@dataclass
class LabelSyncResult:
    """Outcome of one project's label sync. project_page_id lets the worker light
    the status dot; `failed` (any mailbox that errored) decides done-vs-retry."""

    project_page_id: str
    label_name: str | None = None
    action: str = "skipped"  # created | synced | unchanged | skipped
    created: list[str] = field(default_factory=list)
    patched: list[str] = field(default_factory=list)
    healed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    nas_action: str = "skipped"
    note: str | None = None  # set when there's nothing to do (e.g. no title)


async def _create_label_for_all_users(
    notion_page_id: str, label_name: str
) -> dict[str, list[str]]:
    """Create the label in every active user's mailbox AND record the (project, user) → label_id
    mapping so a future rename can target each label by ID.

    Returns lists of user emails per outcome: `created` minted a fresh label,
    `already_present` was an idempotent no-op (label was already in that
    mailbox), `failed` errored. Per-user work runs concurrently on the thread
    pool — a slow or failing mailbox doesn't hold up the rest.
    """
    async with SessionLocal() as session:
        users = (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()

    created: list[str] = []
    already_present: list[str] = []
    failed: list[str] = []
    label_ids_by_user: dict[str, str] = {}
    loop = asyncio.get_running_loop()

    async def _one(email: str) -> None:
        try:
            label = await loop.run_in_executor(
                _executor, partial(gmail_client.create_label, email, label_name)
            )
            if label.get("_created"):
                created.append(email)
            else:
                already_present.append(email)
            label_ids_by_user[email] = label["id"]
        except Exception:
            logger.exception("Failed to create label %r for %s", label_name, email)
            failed.append(email)

    await asyncio.gather(*(_one(u.email) for u in users))

    if label_ids_by_user:
        async with SessionLocal() as session:
            for email, label_id in label_ids_by_user.items():
                stmt = (
                    pg_insert(ProjectLabel)
                    .values(
                        notion_page_id=notion_page_id,
                        user_email=email,
                        gmail_label_id=label_id,
                        current_name=label_name,
                    )
                    .on_conflict_do_update(
                        index_elements=["notion_page_id", "user_email"],
                        set_={"gmail_label_id": label_id, "current_name": label_name},
                    )
                )
                await session.execute(stmt)
            await session.commit()

    return {"created": created, "already_present": already_present, "failed": failed}


async def _reconcile_label_for_all_users(
    notion_page_id: str, expected_name: str
) -> dict[str, Any]:
    """Force every active user's Gmail label for this project to match `expected_name`.

    Notion is the source of truth. For each stored (project, user) row we fetch
    the live label by its stored ID, compare to `expected_name`, and patch back
    if they differ. Catches three drift cases in one place:

    1. Project renamed in Notion → DB row stale → live label still old → patch.
    2. Label renamed manually in Gmail → row "looks fine" but the live label
       diverged → patch.
    3. Label deleted in Gmail → labels.get 404 → create-by-name + rewrite the row
       with the fresh ID (self-heal).

    Per-row work runs concurrently on the thread pool. Returns
    {patched, healed, unchanged, failed} (lists of emails); `no_mapping` True
    when there are no rows yet (first sync of this project).
    """
    async with SessionLocal() as session:
        rows = list(
            (
                await session.execute(
                    select(ProjectLabel).where(ProjectLabel.notion_page_id == notion_page_id)
                )
            )
            .scalars()
            .all()
        )

    if not rows:
        return {"patched": [], "healed": [], "unchanged": [], "failed": [], "no_mapping": True}

    patched: list[str] = []
    healed_ids: dict[str, str] = {}
    unchanged: list[str] = []
    failed: list[str] = []
    loop = asyncio.get_running_loop()

    async def _one(row: ProjectLabel) -> None:
        try:
            live = await loop.run_in_executor(
                _executor,
                partial(gmail_client.get_label, row.user_email, row.gmail_label_id),
            )
        except HttpError as err:
            if err.resp.status == 404:
                logger.warning(
                    "Stale label %s for %s (page=%s); creating by name and healing the row",
                    row.gmail_label_id,
                    row.user_email,
                    notion_page_id,
                )
                try:
                    label = await loop.run_in_executor(
                        _executor,
                        partial(gmail_client.create_label, row.user_email, expected_name),
                    )
                    healed_ids[row.user_email] = label["id"]
                    return
                except Exception:
                    logger.exception(
                        "Self-heal failed for %s (page=%s)", row.user_email, notion_page_id
                    )
                    failed.append(row.user_email)
                    return
            logger.exception(
                "Failed to fetch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)
            return
        except Exception:
            logger.exception(
                "Failed to fetch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)
            return

        if live.get("name") == expected_name:
            unchanged.append(row.user_email)
            return

        logger.info(
            "↳ drift detected for %s: live=%r expected=%r; patching",
            row.user_email,
            live.get("name"),
            expected_name,
        )
        try:
            await loop.run_in_executor(
                _executor,
                partial(
                    gmail_client.update_label_name,
                    row.user_email,
                    row.gmail_label_id,
                    expected_name,
                ),
            )
            patched.append(row.user_email)
        except Exception:
            logger.exception(
                "Failed to patch label %s for %s (page=%s)",
                row.gmail_label_id,
                row.user_email,
                notion_page_id,
            )
            failed.append(row.user_email)

    await asyncio.gather(*(_one(row) for row in rows))

    if patched or healed_ids:
        async with SessionLocal() as session:
            touched = patched + list(healed_ids.keys())
            await session.execute(
                update(ProjectLabel)
                .where(ProjectLabel.notion_page_id == notion_page_id)
                .where(ProjectLabel.user_email.in_(touched))
                .values(current_name=expected_name)
            )
            for email, fresh_id in healed_ids.items():
                await session.execute(
                    update(ProjectLabel)
                    .where(ProjectLabel.notion_page_id == notion_page_id)
                    .where(ProjectLabel.user_email == email)
                    .values(gmail_label_id=fresh_id)
                )
            await session.commit()

    return {
        "patched": patched,
        "healed": list(healed_ids.keys()),
        "unchanged": unchanged,
        "failed": failed,
    }


async def _sync_nas_folder_for_project(
    notion_page_id: str,
    title: str,
    created_time: str | None,
    disciplines: list[str] | None = None,
) -> str:
    """Create or rename the project's folder structure on the office NAS.

    `disciplines` are the raw Notion `Disipliner` labels; they decide which
    discipline-conditional branches get created (the NAS layer normalizes and
    filters them). Re-read from Notion each sync — not persisted — so a
    discipline added in Notion gets its folders on the next sync.

    Best-effort and fully decoupled from the Gmail-label step: a down or
    unmounted share (or any filesystem error) is logged and reported, never
    raised. Returns: "created" | "renamed" | "unchanged" | "skipped" | "failed".
    """
    if not (settings.sync_nas_folders and settings.nas_projects_root):
        return "skipped"

    if not await asyncio.to_thread(nas_client.nas_available):
        logger.warning(
            "↳ NAS unavailable (root %r not a writable dir); skipping folder sync",
            settings.nas_projects_root,
        )
        return "failed"

    try:
        async with SessionLocal() as session:
            row = await session.get(ProjectFolder, notion_page_id)

            if row is None:
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders, title, created_time, disciplines
                )
                outcome = "created"
            elif row.current_name != title:
                target = await asyncio.to_thread(
                    nas_client.rename_project_folder,
                    row.current_name,
                    title,
                    created_time,
                    disciplines,
                )
                outcome = "renamed"
            else:
                # Unchanged name — still ensure the structure exists, so a folder
                # a user deleted by hand (or a discipline added in Notion since
                # the last sync) gets healed/created on the next click.
                target = await asyncio.to_thread(
                    nas_client.ensure_project_folders, title, created_time, disciplines
                )
                outcome = "unchanged"

            await session.merge(
                ProjectFolder(
                    notion_page_id=notion_page_id,
                    current_path=str(target),
                    current_name=title,
                )
            )
            await session.commit()
        logger.info("↳ NAS folder %s: %s", outcome, target)
        return outcome
    except Exception:
        logger.exception("↳ NAS folder sync failed for project %r", title)
        return "failed"


async def sync_project_labels(project_page_id: str) -> LabelSyncResult:
    """Reconcile + create this project's Gmail label across every active mailbox,
    then sync the NAS folder. The durable queue worker calls this for a
    `label_sync` task; idempotent, so retries and re-clicks are safe.

    Re-fetches the page so the label name reflects the title AT PROCESSING TIME
    (a rename between click and processing is picked up). The year segment comes
    from Notion's immutable created_time, so renames only change the leaf.
    """
    page = await notion_client.get_page(project_page_id)

    title = notion_client.extract_page_title(page)
    if not title:
        logger.info("↳ page %s has no title yet, skipping label sync", project_page_id)
        return LabelSyncResult(project_page_id=project_page_id, note="no title yet")

    created_time = page.get("created_time")
    label_name = project_label_path(title, created_time)

    result = LabelSyncResult(project_page_id=project_page_id, label_name=label_name)

    if settings.sync_gmail_labels:
        # Order matters: reconcile first (no-op when there are no rows yet, i.e.
        # first sync), then top-up creates for any active user without a row.
        reconcile = await _reconcile_label_for_all_users(project_page_id, label_name)
        topup = await _create_label_for_all_users(project_page_id, label_name)

        result.patched = reconcile["patched"]
        result.healed = reconcile["healed"]
        result.created = topup["created"]
        result.failed = sorted({*reconcile["failed"], *topup["failed"]})

        if reconcile.get("no_mapping"):
            result.action = "created"
        elif reconcile["patched"] or reconcile["healed"]:
            result.action = "synced"
        else:
            result.action = "unchanged"

        logger.info(
            "↳ gmail labels %r: action=%s created=%d patched=%d healed=%d failed=%d",
            label_name,
            result.action,
            len(result.created),
            len(result.patched),
            len(result.healed),
            len(result.failed),
        )
    else:
        logger.info("↳ gmail label sync skipped (SYNC_GMAIL_LABELS=false)")

    disciplines = notion_client.disciplines_from_page(page)
    result.nas_action = await _sync_nas_folder_for_project(
        project_page_id, title, created_time, disciplines
    )
    if result.nas_action != "skipped":
        logger.info("↳ NAS folder %s", result.nas_action)

    return result
