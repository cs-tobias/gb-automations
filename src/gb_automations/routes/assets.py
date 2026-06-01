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

# Pre-baked solid-black PNG used as a last-resort fallback when
# `render_placeholder` raises. Generated once at import time so the
# endpoint can serve a valid image without re-entering PIL on the
# error path. Frame storing a plain-black tile is far better than
# Frame storing a 500-error HTML body as the file content — the
# operator can tell black-from-broken at a glance.
_FALLBACK_PNG = render_placeholder(None, None)


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

    # Bulletproof: any PIL failure (font cache race under bulk-concurrency,
    # missing glyph, OOM, …) MUST NOT propagate as a 500 — Frame would store
    # the error body as the file content and the deliverable shows a broken
    # preview. Fall back to the pre-baked solid-black PNG so the endpoint
    # always returns valid PNG bytes.
    try:
        png = await asyncio.to_thread(render_placeholder, text, bg_bytes)
    except Exception:
        logger.error(
            "🖼  placeholder: render_placeholder raised for %s — "
            "returning fallback PNG (Frame would otherwise store an error body)",
            deliverable_page_id,
            exc_info=True,
        )
        png = _FALLBACK_PNG

    # One log line per render — surfaces which deliverables Frame actually
    # fetches under a bulk-Sync fan-out, and what bytes we sent. Cross-
    # reference against any broken-preview reports: missing log line →
    # Frame never reached us (tunnel issue); log line present with normal
    # png size → bytes were fine, problem is downstream; log line present
    # with png size == fallback size → renderer raised (see ERROR above).
    text_preview = (
        text if not text else (text[:60] + "…" if len(text) > 60 else text)
    )
    logger.info(
        "🖼  placeholder render for %s: text=%r bg_bytes=%d → png=%d bytes",
        deliverable_page_id,
        text_preview,
        len(bg_bytes) if bg_bytes else 0,
        len(png),
    )

    # No-cache: the description/thumbnail can change between provisionings, and
    # Frame only fetches once per upload anyway, so a stale cache buys nothing.
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )
