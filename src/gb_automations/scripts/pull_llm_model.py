"""Pull the configured Ollama model into the local volume.

Run once after the first `docker compose up -d` so the model is available
before the api tries to call it. Idempotent — pulling an already-present
model is a fast no-op.

Usage (inside the container):
    docker compose exec api python -m gb_automations.scripts.pull_llm_model

Streams Ollama's progress lines to stdout so you can watch the ~5GB download.
Re-run after changing OLLAMA_MODEL in .env.
"""

import asyncio
import json
import sys

import httpx

from gb_automations.config import settings


async def pull() -> int:
    url = f"{settings.ollama_base_url.rstrip('/')}/api/pull"
    body = {"name": settings.ollama_model, "stream": True}

    print(f"Pulling model {settings.ollama_model!r} from {settings.ollama_base_url} …")
    last_status = ""
    # No overall timeout — large model downloads can take many minutes.
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream("POST", url, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        print(line)
                        continue
                    status = evt.get("status") or ""
                    if "completed" in evt and "total" in evt:
                        pct = (evt["completed"] / evt["total"]) * 100 if evt["total"] else 0
                        print(f"  {status}: {pct:5.1f}%", end="\r", flush=True)
                    elif status and status != last_status:
                        print(f"  {status}")
                        last_status = status
                    if evt.get("error"):
                        print(f"\nERROR: {evt['error']}", file=sys.stderr)
                        return 1
        except httpx.HTTPError as err:
            print(f"\nFailed to reach Ollama at {url}: {err}", file=sys.stderr)
            print(
                "Hint: Ollama runs natively on the host, not in Docker. "
                "Is `ollama serve` running on the host (port 11434)?",
                file=sys.stderr,
            )
            return 1

    print(f"\nDone. Model {settings.ollama_model!r} is ready.")
    return 0


def main() -> None:
    sys.exit(asyncio.run(pull()))


if __name__ == "__main__":
    main()
