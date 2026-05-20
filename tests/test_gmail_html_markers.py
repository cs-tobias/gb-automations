"""Tests for the HTML→[image: NAME] marker injection in clients/gmail.py.

The HTML-only path is rare but critical: forwards originating from Outlook
arrive without a text/plain part, so `_strip_html` drops `<img>` tags entirely.
Without the marker-injection step, downstream attachment attribution can't tell
which forwarded sub-message references which inline image, and everything
falls onto the empty forwarder row.
"""

import base64

from gb_automations.clients.gmail import (
    GmailAttachment,
    _count_inline_image_refs,
    _extract_body_and_attachments,
    _inject_inline_image_markers,
    _normalize_content_id,
    _synthesize_inline_filename,
    decode_inline_data,
)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def test_normalize_content_id_strips_angle_brackets_and_lowercases():
    assert _normalize_content_id("<Foo.Bar@host>") == "foo.bar@host"
    assert _normalize_content_id("plain-token") == "plain-token"
    assert _normalize_content_id("") is None
    assert _normalize_content_id("<>") is None


def test_inject_markers_resolves_cid_by_content_id():
    html = '<p>Hei</p><img src="cid:abc.123@host"><p>Slutt</p>'
    atts = [
        GmailAttachment(
            filename="logo.png",
            mime_type="image/png",
            size=100,
            attachment_id="A",
            content_id="abc.123@host",
        )
    ]
    out = _inject_inline_image_markers(html, atts)
    assert "[image: logo.png]" in out


def test_inject_markers_resolves_outlook_style_filename_prefix_cid():
    # Outlook embeds inline images with the filename as the cid prefix and
    # a per-message host suffix. The content_id header may be missing.
    html = '<img src="cid:image001.png@01D1234.5678">'
    atts = [
        GmailAttachment(
            filename="image001.png",
            mime_type="image/png",
            size=100,
            attachment_id="A",
            content_id=None,
        )
    ]
    out = _inject_inline_image_markers(html, atts)
    assert "[image: image001.png]" in out


def test_inject_markers_preserves_document_order():
    html = (
        '<img src="cid:image001.png@x">'
        '<p>middle paragraph</p>'
        '<img src="cid:image002.png@x">'
    )
    atts = [
        GmailAttachment("image001.png", "image/png", 100, "A1", "image001.png@x"),
        GmailAttachment("image002.png", "image/png", 100, "A2", "image002.png@x"),
    ]
    out = _inject_inline_image_markers(html, atts)
    pos1 = out.index("[image: image001.png]")
    pos2 = out.index("[image: image002.png]")
    assert pos1 < pos2


def test_inject_markers_leaves_unresolvable_srcs_alone():
    html = '<img src="https://example.com/foo.png"><img src="data:image/png;base64,iVBORw">'
    atts = [GmailAttachment("logo.png", "image/png", 100, "A", "x@y")]
    out = _inject_inline_image_markers(html, atts)
    assert "[image:" not in out
    assert "example.com" in out  # untouched


def test_extract_body_html_only_path_injects_markers():
    # Build a minimal Gmail-style payload: multipart with HTML body + one
    # inline image attachment carrying a Content-ID header.
    payload = {
        "mimeType": "multipart/related",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {
                    "data": _b64(
                        '<p>Forwarded body</p>'
                        '<img src="cid:image001.png@01D">'
                        '<p>After image</p>'
                    )
                },
            },
            {
                "mimeType": "image/png",
                "filename": "image001.png",
                "headers": [{"name": "Content-ID", "value": "<image001.png@01D>"}],
                "body": {"size": 1234, "attachmentId": "ATT_1"},
            },
        ],
    }
    body, atts = _extract_body_and_attachments(payload)
    assert "[image: image001.png]" in body
    assert len(atts) == 1
    assert atts[0].filename == "image001.png"
    assert atts[0].content_id == "image001.png@01d"


def test_count_inline_refs_real_thread_pattern():
    """Reproduces the live `19e2b76ceaf9facd` thread shape: HTML references
    image001.png 3 times (one per Ingeborg signature block) and image002.png
    once (a single real photo). The counter should produce 3 and 1."""
    html = (
        '<p>Hei</p>'
        '<img src="cid:ii_a@x">'  # image001 ref 1
        '<p>body</p>'
        '<img src="cid:ii_b@x">'  # image002 ref 1
        '<p>quoted reply</p>'
        '<img src="cid:ii_a@x">'  # image001 ref 2
        '<p>more</p>'
        '<img src="cid:ii_a@x">'  # image001 ref 3
    )
    sig = GmailAttachment("image001.png", "image/png", 4_761, "A1", "ii_a@x")
    photo = GmailAttachment("image002.png", "image/png", 443_000, "A2", "ii_b@x")
    _count_inline_image_refs(html, [sig, photo])
    assert sig.inline_ref_count == 3
    assert photo.inline_ref_count == 1


def test_count_inline_refs_idempotent():
    """Re-running the counter must not double-count — counts are reset
    inside the function before scanning."""
    html = '<img src="cid:ii_a@x"><img src="cid:ii_a@x">'
    att = GmailAttachment("logo.png", "image/png", 100, "A", "ii_a@x")
    _count_inline_image_refs(html, [att])
    _count_inline_image_refs(html, [att])
    assert att.inline_ref_count == 2


def test_count_inline_refs_resolves_outlook_filename_prefix_cid():
    """Counter resolution mirrors marker injection: when Content-ID is
    missing, Outlook-style cid tokens that start with the filename still
    resolve."""
    html = '<img src="cid:image001.png@01D"><img src="cid:image001.png@01D">'
    att = GmailAttachment("image001.png", "image/png", 100, "A", None)
    _count_inline_image_refs(html, [att])
    assert att.inline_ref_count == 2


def test_count_inline_refs_ignores_non_cid_srcs():
    html = '<img src="https://example.com/foo.png"><img src="data:image/png;base64,iVBO">'
    att = GmailAttachment("logo.png", "image/png", 100, "A", "ii@x")
    _count_inline_image_refs(html, [att])
    assert att.inline_ref_count == 0


def test_extract_body_uses_html_when_both_present():
    """HTML wins whenever it's available — even with text/plain alongside.
    Outlook's text/plain alternative drops inline image references, so
    sourcing from HTML is the only way to preserve `[image: NAME]` anchors
    in their original positions. inline_ref_count is populated either way."""
    import base64

    def _b64(s: str) -> str:
        return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")

    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Plain body without markers.")},
            },
            {
                "mimeType": "text/html",
                "body": {"data": _b64(
                    '<p>Hi</p>'
                    '<img src="cid:logo@x">'
                    '<p>reply</p>'
                    '<img src="cid:logo@x">'
                )},
            },
            {
                "mimeType": "image/png",
                "filename": "logo.png",
                "headers": [{"name": "Content-ID", "value": "<logo@x>"}],
                "body": {"size": 100, "attachmentId": "ATT_1"},
            },
        ],
    }
    body, atts = _extract_body_and_attachments(payload)
    # HTML wins, so we see the injected markers.
    assert body.count("[image: logo.png]") == 2
    assert "Hi" in body
    assert "reply" in body
    assert len(atts) == 1
    assert atts[0].inline_ref_count == 2


def test_extract_body_falls_back_to_plain_when_html_absent():
    """No HTML → text/plain is used directly, unchanged."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {
                "mimeType": "text/plain",
                "body": {"data": _b64("Hei,\nMvh\nMe")},
            },
        ],
    }
    body, _atts = _extract_body_and_attachments(payload)
    assert "Hei," in body
    assert "Mvh" in body


# --- Inline image recovery (D1 + D2) ---------------------------------------
# Regression: Outlook inline photos arrive either (D1) with a Content-ID but
# NO filename, or (D2) with the bytes inline in body.data and NO attachmentId.
# Both used to be silently dropped, losing real images.


def test_synthesize_inline_filename_from_content_id():
    # cid stem before @host, extension from mime.
    assert _synthesize_inline_filename("abc123@host", "image/png", 1) == "abc123.png"
    assert _synthesize_inline_filename("photo@01D", "image/jpeg", 1) == "photo.jpg"


def test_synthesize_inline_filename_counter_fallback():
    # No usable cid → per-message counter.
    assert _synthesize_inline_filename(None, "image/png", 3) == "inline-image-3.png"
    assert _synthesize_inline_filename("@host", "image/gif", 2) == "inline-image-2.gif"


def test_synthesize_inline_filename_unknown_mime_falls_back_to_bin():
    assert _synthesize_inline_filename("x@y", "application/octet-stream", 1) == "x.bin"


def test_extract_captures_inline_image_without_filename():
    """D1: an image part with a Content-ID but an empty filename is captured
    (and given a synthesized filename) rather than dropped."""
    payload = {
        "mimeType": "multipart/related",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": _b64('<p>Se bildet</p><img src="cid:img7@host">')},
            },
            {
                "mimeType": "image/png",
                "filename": "",  # Outlook omits the filename on inline parts
                "headers": [{"name": "Content-ID", "value": "<img7@host>"}],
                "body": {"size": 8000, "attachmentId": "ATT_X"},
            },
        ],
    }
    body, atts = _extract_body_and_attachments(payload)
    assert len(atts) == 1
    assert atts[0].filename == "img7.png"
    assert atts[0].content_id == "img7@host"
    # The injected marker uses the synthesized filename, so attribution still works.
    assert "[image: img7.png]" in body


def test_extract_captures_inline_data_when_no_attachment_id():
    """D2: an inline part carrying bytes in body.data with no attachmentId
    keeps those bytes on inline_data so the upload path can decode them."""
    raw = b"\x89PNG\r\n\x1a\n fake png bytes"
    payload = {
        "mimeType": "multipart/related",
        "parts": [
            {
                "mimeType": "text/html",
                "body": {"data": _b64('<img src="cid:inlinepic@h">')},
            },
            {
                "mimeType": "image/png",
                "filename": "",
                "headers": [{"name": "Content-ID", "value": "<inlinepic@h>"}],
                "body": {
                    "size": len(raw),
                    "data": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
                    # no attachmentId
                },
            },
        ],
    }
    _body, atts = _extract_body_and_attachments(payload)
    assert len(atts) == 1
    assert atts[0].attachment_id is None
    assert atts[0].inline_data is not None
    assert decode_inline_data(atts[0].inline_data) == raw


def test_extract_does_not_capture_text_or_container_parts_as_attachments():
    """Text bodies and multipart containers must never be mistaken for
    inline attachments even though they lack a filename."""
    payload = {
        "mimeType": "multipart/alternative",
        "parts": [
            {"mimeType": "text/plain", "body": {"data": _b64("hei")}},
            {"mimeType": "text/html", "body": {"data": _b64("<p>hei</p>")}},
        ],
    }
    _body, atts = _extract_body_and_attachments(payload)
    assert atts == []
