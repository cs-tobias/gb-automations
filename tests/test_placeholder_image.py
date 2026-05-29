from io import BytesIO

from PIL import Image

from gb_automations.sync.placeholder_image import (
    CANVAS_H,
    CANVAS_W,
    render_placeholder,
)

_PNG_MAGIC = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])


def _assert_valid_png(data: bytes) -> Image.Image:
    assert data[:8] == _PNG_MAGIC
    img = Image.open(BytesIO(data))
    img.load()
    assert img.size == (CANVAS_W, CANVAS_H)
    return img


def test_text_only_black_background():
    img = _assert_valid_png(render_placeholder("Vinkel 1 - stue", None))
    # Solid black somewhere in a corner, white text pixels somewhere in the middle.
    assert img.convert("RGB").getpixel((5, 5)) == (0, 0, 0)


def test_empty_text_renders_plain_canvas():
    _assert_valid_png(render_placeholder("", None))
    _assert_valid_png(render_placeholder(None, None))


def test_with_background_image():
    src = Image.new("RGB", (640, 480), (200, 50, 50))
    buf = BytesIO()
    src.save(buf, "PNG")
    img = _assert_valid_png(render_placeholder("Reference", buf.getvalue()))
    # The red background is darkened by the scrim but still reddish (not black).
    r, g, b = img.convert("RGB").getpixel((5, 5))
    assert r > g and r > b and r > 0


def test_garbage_background_falls_back_to_black():
    img = _assert_valid_png(render_placeholder("x", b"not an image"))
    assert img.convert("RGB").getpixel((5, 5)) == (0, 0, 0)


def test_long_text_still_fits_and_renders():
    long = "Lorem ipsum dolor sit amet " * 40
    _assert_valid_png(render_placeholder(long, None))
