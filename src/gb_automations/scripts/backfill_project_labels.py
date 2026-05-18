"""Backfill the project_labels table for projects that existed before this feature shipped.

For every Notion project × every active user, look up the existing Gmail label
by its current name and upsert a (notion_page_id, user_email) → gmail_label_id row.

Usage (inside the container):
    docker compose exec api python -m gb_automations.scripts.backfill_project_labels

Idempotent — safe to run multiple times. Run once after deploying the rename feature
and any time you suspect the mapping has drifted (e.g. labels were renamed manually
in Gmail, bypassing the webhook).
"""

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from gb_automations.clients import gmail as gmail_client
from gb_automations.clients import notion as notion_client
from gb_automations.db import SessionLocal
from gb_automations.models import ProjectLabel, User

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
logger = logging.getLogger("backfill_project_labels")


async def backfill() -> None:
    projects = await notion_client.get_project_pages()
    if not projects:
        logger.warning("No projects found in Notion. Nothing to backfill.")
        return

    async with SessionLocal() as session:
        users = (
            (await session.execute(select(User).where(User.active.is_(True)))).scalars().all()
        )

        if not users:
            logger.warning("No active users in DB. Run seed_users first.")
            return

        upserted = 0
        missing: list[tuple[str, str]] = []  # (label_name, user_email)

        # Keys of `projects` are the full nested Gmail label paths
        # ("Projects/<year>/<title>"); values carry the Notion page ID + title.
        for label_name, meta in projects.items():
            page_id = meta["id"]
            for user in users:
                label = gmail_client.find_label_by_name(user.email, label_name)
                if not label:
                    missing.append((label_name, user.email))
                    continue
                stmt = (
                    insert(ProjectLabel)
                    .values(
                        notion_page_id=page_id,
                        user_email=user.email,
                        gmail_label_id=label["id"],
                        current_name=label_name,
                    )
                    .on_conflict_do_update(
                        index_elements=["notion_page_id", "user_email"],
                        set_={
                            "gmail_label_id": label["id"],
                            "current_name": label_name,
                        },
                    )
                )
                await session.execute(stmt)
                upserted += 1
                logger.info("  %s × %s → %s", label_name, user.email, label["id"])

        await session.commit()

    logger.info(
        "Done. upserted=%d missing=%d (projects=%d × users=%d = %d expected)",
        upserted,
        len(missing),
        len(projects),
        len(users),
        len(projects) * len(users),
    )
    for label_name, email in missing:
        logger.warning("  missing label %r in mailbox %s", label_name, email)


def main() -> None:
    asyncio.run(backfill())


if __name__ == "__main__":
    main()
