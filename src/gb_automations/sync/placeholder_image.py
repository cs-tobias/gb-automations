"""Render the Frame.io V00 placeholder image on the fly.

The placeholder used to be a single static logo; now each deliverable gets its
own image = (uploaded reference background, or solid black) + the deliverable's
description text drawn on top. The render endpoint serves the bytes Frame
fetches via `create_file_from_url`.

Pure, synchronous, CPU-bound — call via `asyncio.to_thread` so the event loop
isn't blocked (repo convention). Never raises on bad input: a blank/garbled
background or empty text degrades to the plain black canvas, so Frame never
fetches a broken file.
"""

from __future__ import annotations

import logging
import os
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# Square (1:1). Fixed size keeps text sizing and wrapping predictable
# regardless of the uploaded background's dimensions.
CANVAS_W = 1080
CANVAS_H = 1080

_BG_COLOR = (0, 0, 0)
_TEXT_COLOR = (255, 255, 255)
# Slight scrim over an uploaded background so white text stays legible on a
# bright reference image. 0 = no darkening, 255 = full black.
_SCRIM_ALPHA = 110

_FONT_PATH = os.path.join(
    os.path.dirname(__file__), "..", "assets", "DejaVuSans-Bold.ttf"
)
_MAX_FONT_SIZE = 120
_MIN_FONT_SIZE = 28
# Text is laid out inside this fraction of the canvas, centered, leaving a
# margin so it never touches the edges.
_TEXT_BOX_FRAC = 0.82


def render_placeholder(text: str | None, bg_bytes: bytes | None) -> bytes:
    """Render a JPEG placeholder. Background = `bg_bytes` (cover-cropped to the
    canvas) or solid black; `text` (when non-empty) is wrapped + centered in
    white over a darkening scrim. Always returns valid JPEG bytes.

    JPEG (not PNG): Goldbox ships all client deliverables as JPG, and the
    placeholder slot is what the studio's first real V01 upload replaces in
    Frame's version stack — keeping the same extension matches their workflow.
    Quality 90 is well above the threshold where text-on-flat-color edges
    show JPEG ringing; bytes are still ~5× smaller than the PNG was.

    Text rendering is wrapped defensively — under bulk-Sync concurrency PIL
    has occasionally raised on shared font handles. We log + skip the text
    overlay rather than fail the whole render; the deliverable then shows
    its background-or-black tile in Frame, still recognizable as the right
    placeholder."""
    canvas = _make_background(bg_bytes)
    cleaned = (text or "").strip()
    if cleaned:
        try:
            _draw_centered_text(canvas, cleaned, scrim=bg_bytes is not None)
        except Exception:
            logger.warning(
                "placeholder: text render failed for %r — keeping bare background",
                cleaned[:60] + "…" if len(cleaned) > 60 else cleaned,
                exc_info=True,
            )
    out = BytesIO()
    canvas.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


def _make_background(bg_bytes: bytes | None) -> Image.Image:
    if bg_bytes:
        try:
            src = Image.open(BytesIO(bg_bytes)).convert("RGB")
            return _cover_crop(src, CANVAS_W, CANVAS_H)
        except Exception:
            logger.warning(
                "placeholder: failed to decode background image, using black",
                exc_info=True,
            )
    return Image.new("RGB", (CANVAS_W, CANVAS_H), _BG_COLOR)


def _cover_crop(img: Image.Image, w: int, h: int) -> Image.Image:
    """Scale `img` to cover w×h (preserving aspect) and center-crop the
    overflow — the same behavior as CSS `background-size: cover`."""
    src_w, src_h = img.size
    scale = max(w / src_w, h / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return img.crop((left, top, left + w, top + h))


def _draw_centered_text(canvas: Image.Image, text: str, *, scrim: bool) -> None:
    if scrim:
        overlay = Image.new("RGBA", canvas.size, (0, 0, 0, _SCRIM_ALPHA))
        canvas.paste(Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB"))

    draw = ImageDraw.Draw(canvas)
    box_w = int(CANVAS_W * _TEXT_BOX_FRAC)
    box_h = int(CANVAS_H * _TEXT_BOX_FRAC)

    font, lines = _fit_text(draw, text, box_w, box_h)
    line_h = _line_height(draw, font)
    total_h = line_h * len(lines)
    y = (CANVAS_H - total_h) // 2
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        line_w = bbox[2] - bbox[0]
        x = (CANVAS_W - line_w) // 2
        draw.text((x, y), line, fill=_TEXT_COLOR, font=font)
        y += line_h


def _fit_text(
    draw: ImageDraw.ImageDraw, text: str, box_w: int, box_h: int
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Find the largest font size at which `text`, word-wrapped to `box_w`,
    also fits within `box_h`. Falls back to the minimum size (text may clip on
    pathologically long input — acceptable for a placeholder)."""
    size = _MAX_FONT_SIZE
    while size >= _MIN_FONT_SIZE:
        font = ImageFont.truetype(_FONT_PATH, size)
        lines = _wrap(draw, text, font, box_w)
        total_h = _line_height(draw, font) * len(lines)
        if total_h <= box_h:
            return font, lines
        size -= 6
    font = ImageFont.truetype(_FONT_PATH, _MIN_FONT_SIZE)
    return font, _wrap(draw, text, font, box_w)


def _wrap(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    box_w: int,
) -> list[str]:
    """Greedy word-wrap to `box_w`. A single word wider than the box is kept on
    its own line (it'll be shrunk by the size search, or clipped at min size)."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= box_w:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> int:
    # Use a tall sample so ascenders/descenders are included; add 20% leading.
    bbox = draw.textbbox((0, 0), "Ahgjpq", font=font)
    return int((bbox[3] - bbox[1]) * 1.2)
