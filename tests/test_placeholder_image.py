from io import BytesIO

from PIL import Image

from gb_automations.sync.placeholder_image import (
    CANVAS_H,
    CANVAS_W,
    render_placeholder,
)

# JPEG SOI marker (Start Of Image). Every valid JPEG starts with FF D8 FF.
_JPEG_MAGIC = bytes([0xFF, 0xD8, 0xFF])


def _assert_valid_jpeg(data: bytes) -> Image.Image:
    assert data[:3] == _JPEG_MAGIC
    img = Image.open(BytesIO(data))
    img.load()
    assert img.size == (CANVAS_W, CANVAS_H)
    assert img.format == "JPEG"
    return img


def test_text_only_black_background():
    img = _assert_valid_jpeg(render_placeholder("Vinkel 1 - stue", None))
    # Solid black somewhere in a corner. JPEG is lossy on flat blocks but the
    # quantization for pure (0,0,0) is essentially exact — allow a sliver of
    # tolerance against future quality changes.
    r, g, b = img.convert("RGB").getpixel((5, 5))
    assert r < 5 and g < 5 and b < 5


def test_empty_text_renders_plain_canvas():
    _assert_valid_jpeg(render_placeholder("", None))
    _assert_valid_jpeg(render_placeholder(None, None))


def test_with_background_image():
    src = Image.new("RGB", (640, 480), (200, 50, 50))
    buf = BytesIO()
    src.save(buf, "PNG")
    img = _assert_valid_jpeg(render_placeholder("Reference", buf.getvalue()))
    # The red background is darkened by the scrim but still reddish (not black).
    r, g, b = img.convert("RGB").getpixel((5, 5))
    assert r > g and r > b and r > 0


def test_garbage_background_falls_back_to_black():
    img = _assert_valid_jpeg(render_placeholder("x", b"not an image"))
    r, g, b = img.convert("RGB").getpixel((5, 5))
    assert r < 5 and g < 5 and b < 5


def test_long_text_still_fits_and_renders():
    long = "Lorem ipsum dolor sit amet " * 40
    _assert_valid_jpeg(render_placeholder(long, None))
