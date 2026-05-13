"""One-off: start Gmail watches for every active user.

Run after creating the Pub/Sub topic + push subscription (Stage 4c setup).
The APScheduler weekly job will keep them renewed; this is for initial bootstrap
or manual recovery.

Usage (inside the container):
    docker compose exec api python -m gb_automations.scripts.start_watches
"""

import asyncio
import json
import logging

from gb_automations.sync.watches import renew_all_watches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


async def main() -> None:
    results = await renew_all_watches()
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
