"""Seed / reconcile the mailboxes the backend syncs into the `users` table.

The authoritative source is the SYNCED_MAILBOXES env var (see config.py). The
app calls reconcile_users(settings.synced_mailbox_list) on startup, so a fresh
`docker compose up` self-seeds — no manual step needed.

This module is still runnable as a CLI for ad-hoc use:

    # reconcile from the env var (same as startup does)
    docker compose exec api python -m gb_automations.scripts.seed_users

    # reconcile to an explicit list (overrides the env var for this run)
    docker compose exec api python -m gb_automations.scripts.seed_users \
        petter@goldbox.no anna@goldbox.no
"""

import asyncio
import logging
import sys

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from gb_automations.config import settings
from gb_automations.db import SessionLocal
from gb_automations.models import User

logger = logging.getLogger(__name__)


async def reconcile_users(emails: list[str]) -> dict:
    """Make the `users` table match `emails` exactly.

    Listed emails are upserted active=True; any user active in the table but
    NOT in `emails` is set active=False (we deactivate rather than delete so
    history rows referencing the user stay intact). Returns a summary dict.

    An empty `emails` list deactivates everyone — intentional: blanking
    SYNCED_MAILBOXES is a valid "sync nothing" state, not a no-op. The caller
    (startup) logs loudly when the list is empty so it's never a silent
    surprise.
    """
    wanted = {e.strip().lower() for e in emails if e.strip()}

    async with SessionLocal() as session:
        for email in wanted:
            stmt = (
                insert(User)
                .values(email=email, active=True)
                .on_conflict_do_update(index_elements=["email"], set_={"active": True})
            )
            await session.execute(stmt)

        # Deactivate any currently-active user no longer in the list.
        active_now = (
            await session.execute(select(User.email).where(User.active.is_(True)))
        ).scalars().all()
        to_deactivate = [e for e in active_now if e not in wanted]
        if to_deactivate:
            await session.execute(
                update(User).where(User.email.in_(to_deactivate)).values(active=False)
            )

        await session.commit()

    return {"active": sorted(wanted), "deactivated": sorted(to_deactivate)}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    cli_emails = sys.argv[1:]
    emails = cli_emails if cli_emails else settings.synced_mailbox_list
    if not emails:
        print(
            "No mailboxes given and SYNCED_MAILBOXES is empty. "
            "Pass emails as args or set SYNCED_MAILBOXES in .env.",
            file=sys.stderr,
        )
        sys.exit(1)
    result = asyncio.run(reconcile_users(emails))
    print(f"active:      {', '.join(result['active']) or '(none)'}")
    print(f"deactivated: {', '.join(result['deactivated']) or '(none)'}")


if __name__ == "__main__":
    main()
