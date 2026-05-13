"""Webhook receivers.

For Stage 4a only the `/webhooks/echo` route exists — it logs whatever is
posted to it so we can prove the Cloudflare Tunnel is wired up before adding
real handlers. Notion and Gmail handlers land in the next chunks (4b, 4c).
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/echo")
@router.get("/echo")
async def echo(request: Request) -> dict[str, Any]:
    """Logs and returns the request — used to verify Cloudflare Tunnel forwarding.

    Accepts both GET and POST so you can quickly check it from a browser too.
    """
    body_bytes = await request.body()
    try:
        body: Any = json.loads(body_bytes) if body_bytes else None
    except json.JSONDecodeError:
        body = body_bytes.decode("utf-8", errors="replace")

    payload = {
        "method": request.method,
        "url": str(request.url),
        "headers": dict(request.headers),
        "body": body,
    }
    logger.info("Echo webhook: %s", json.dumps(payload, default=str)[:2000])
    return {"received": True, **payload}
