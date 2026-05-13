"""Seed user emails into the users table.

Usage (inside the container):
    docker compose exec api python -m gb_automations.scripts.seed_users \
        tobias@tobiaseek.com other@tobiaseek.com
"""

import asyncio
import sys

from sqlalchemy.dialects.postgresql import insert

from gb_automations.db import SessionLocal
from gb_automations.models import User


async def seed(emails: list[str]) -> None:
    if not emails:
        print("No emails provided. Pass them as args.", file=sys.stderr)
        sys.exit(1)

    async with SessionLocal() as session:
        for email in emails:
            stmt = (
                insert(User)
                .values(email=email.lower(), active=True)
                .on_conflict_do_update(index_elements=["email"], set_={"active": True})
            )
            await session.execute(stmt)
            print(f"  upserted: {email.lower()}")
        await session.commit()
    print(f"Done. Seeded {len(emails)} user(s).")


def main() -> None:
    asyncio.run(seed(sys.argv[1:]))


if __name__ == "__main__":
    main()
