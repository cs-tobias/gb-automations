"""Dynamic placeholder image — the per-deliverable Frame.io V00 background.

Frame.io's `create_file_from_url` fetches the placeholder bytes from a URL we
host. Instead of one static logo, we point Frame at
`/assets/placeholder/{deliverable_page_id}.png`, which renders on demand from
the deliverable's Notion row: the `Beskrivelse` text (falling back to the row
title) drawn over the `Thumbnail` upload (or a black canvas).

Public + unauthenticated — same exposure as the static `/assets/` mount — so
Frame can fetch it over the Cloudflare tunnel. Frame fetches asynchronously
*after* the upload call returns, so the URL must be self-contained (it carries
the page id and re-reads Notion at GET time). On ANY error we still return a
plain black PNG with a 200, never a 4xx/5xx: a failed fetch would otherwise
leave a broken V00 file in Frame.
"""

import asyncio
import logging

import httpx
from fastapi import APIRouter
from fastapi.responses import Response

from gb_automations.clients import notion as notion_client
from gb_automations.config import OPPGAVER_DESC_PROP, OPPGAVER_THUMB_PROP
from gb_automations.sync.placeholder_image import render_placeholder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/assets", tags=["assets"])

# Cap the background fetch so a slow/huge Notion-hosted file can't hang the
# render (Frame has its own fetch timeout on the other side).
_BG_FETCH_TIMEOUT = 15.0
_BG_MAX_BYTES = 25 * 1024 * 1024


async def _fetch_bg(url: str) -> bytes | None:
    try:
        async with httpx.AsyncClient(timeout=_BG_FETCH_TIMEOUT) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
        if len(data) > _BG_MAX_BYTES:
            logger.warning(
                "placeholder bg too large (%d bytes) — ignoring", len(data)
            )
            return None
        return data
    except Exception:
        logger.warning("placeholder: background fetch failed", exc_info=True)
        return None


@router.get("/placeholder/{deliverable_page_id}.png")
async def placeholder_png(deliverable_page_id: str) -> Response:
    text: str | None = None
    bg_bytes: bytes | None = None
    try:
        page = await notion_client.get_page(deliverable_page_id)
        text = notion_client.read_rich_text_prop(page, OPPGAVER_DESC_PROP)
        if not text:
            text = notion_client.extract_page_title(page)
        thumb_url = notion_client.read_first_file_url(page, OPPGAVER_THUMB_PROP)
        if thumb_url:
            bg_bytes = await _fetch_bg(thumb_url)
    except Exception:
        logger.warning(
            "placeholder: failed to read Notion deliverable %s — rendering blank",
            deliverable_page_id,
            exc_info=True,
        )

    png = await asyncio.to_thread(render_placeholder, text, bg_bytes)
    # No-cache: the description/thumbnail can change between provisionings, and
    # Frame only fetches once per upload anyway, so a stale cache buys nothing.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
