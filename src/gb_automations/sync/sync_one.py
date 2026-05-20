"""One-shot CLI: sync a single Gmail thread into Notion.

Usage (inside the api container):
    docker compose exec api python -m gb_automations.sync.sync_one \
        --email tobias@tobiaseek.com \
        --thread <gmail_thread_id>

The thread ID is the same one shown in /debug/inbox (the `id` field of each message).
For a thread, you can use any message ID in that thread — Gmail's thread_id property
on that message is what we want.
"""

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict

from gb_automations.sync.sync_thread import sync_thread

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Sync one Gmail thread into Notion.")
    p.add_argument("--email", required=True, help="Workspace user to impersonate (DWD)")
    p.add_argument("--thread", required=True, help="Gmail thread ID to sync")
    p.add_argument(
        "--debug",
        action="store_true",
        help="Verbose DEBUG logging (MIME-part walk, attachment partitioning)",
    )
    return p.parse_args()


async def main() -> int:
    args = parse_args()
    if args.debug:
        logging.getLogger("gb_automations").setLevel(logging.DEBUG)
    result = await sync_thread(args.email, args.thread)
    print(json.dumps(asdict(result), indent=2))
    return 0 if not result.errors else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
