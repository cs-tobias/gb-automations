"""Async client for the local Ollama LLM service.

Public surface: `classify(prompt, allowed_values)` — short-text tag selection.

The LLM is reserved for *judgment* work in this codebase (tagging today,
summarization/cleanup later). Structural work like splitting forwarded email
chains is regex-based — see `utils/history_extraction.py`. Keeping the model
out of the hot path means sync is fast, deterministic, and offline-friendly.

Streaming is used so the per-chunk timeout catches a stalled Ollama (no
progress for N seconds) rather than the whole-response one. Failures (network,
timeout, malformed output) are caught here and surfaced as an empty result;
callers treat tagging as best-effort.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from gb_automations.config import settings
from gb_automations.utils.email_cleaning import clean_body
from gb_automations.utils.email_splitting import ExtractedMessage

logger = logging.getLogger(__name__)


# ============================================================
# Low-level streaming chat helper
# ============================================================


async def _chat_streaming(
    *,
    messages: list[dict[str, str]],
    format_schema: dict[str, Any],
    options: dict[str, Any],
    timeout_s: float,
) -> tuple[str, dict[str, Any]]:
    """Call Ollama `/api/chat` with stream=true, accumulate content, return (text, final_payload).

    Each Ollama chunk is an NDJSON line with shape:
      {"message": {"role": "assistant", "content": "..."}, "done": false}
    Final chunk has `done: true` and includes token-count fields
    (prompt_eval_count, eval_count, prompt_eval_duration, eval_duration).

    Returns the concatenated `message.content` text and the final (`done: true`)
    payload. Raises `httpx.TimeoutException` if no chunk arrives within
    `timeout_s` seconds, or `TimeoutError` if the total stream exceeds it.
    Callers should catch and fall back gracefully.
    """
    body = {
        "model": settings.ollama_model,
        "messages": messages,
        "stream": True,
        "format": format_schema,
        "options": options,
    }
    timeout = httpx.Timeout(connect=10.0, write=10.0, read=timeout_s, pool=10.0)

    parts: list[str] = []
    final_payload: dict[str, Any] = {}
    start = time.monotonic()

    async with httpx.AsyncClient(base_url=settings.ollama_base_url, timeout=timeout) as client:
        async with client.stream("POST", "/api/chat", json=body) as response:
            response.raise_for_status()
            async for raw_line in response.aiter_lines():
                if time.monotonic() - start > timeout_s:
                    raise TimeoutError(
                        f"Ollama stream exceeded wall-clock cap of {timeout_s}s"
                    )
                if not raw_line:
                    continue
                try:
                    chunk = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.warning("Ollama emitted non-JSON chunk: %r", raw_line[:200])
                    continue
                msg = chunk.get("message") or {}
                piece = msg.get("content")
                if isinstance(piece, str):
                    parts.append(piece)
                if chunk.get("done"):
                    final_payload = chunk
                    break

    return "".join(parts), final_payload


# ============================================================
# classify — tag selection
# ============================================================


def _load_classify_prompt() -> str:
    """Load the tagging system prompt from the path in settings.

    Relative paths are resolved against the current working directory. In the
    container that's `/app` (Dockerfile sets WORKDIR /app and COPYs `prompts/`
    to `/app/prompts/`); in dev it's the repo root where you run `uv` or
    `pytest` from. Walking up from `__file__` doesn't work once the package is
    installed non-editable into site-packages — the source tree's parent
    layout no longer exists there.
    """
    raw = settings.tagging_prompt_path
    p = Path(raw)
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.read_text(encoding="utf-8")


_CLASSIFY_SYSTEM_PROMPT = _load_classify_prompt()


async def classify(
    prompt: str,
    allowed_values: list[str],
    *,
    sender_role: str = "",
) -> list[str]:
    """Pick a subset of `allowed_values` that best describes `prompt`.

    Uses Ollama's `format` parameter to constrain output to a JSON object
    matching {"tags": ["..."]} with enum values restricted to `allowed_values`.
    Returns the validated tag list, or [] on any failure.

    `sender_role` is an optional hint about who wrote the email. Pass one of:
      - "intern" — sender is on our team (the studio)
      - "ekstern" — sender is a client or external collaborator
      - "" (default) — no role context; the prompt treats the message neutrally
    The directional rules in the system prompt (e.g. leveranse vs underlag,
    "we comment" vs "they comment") need this to apply correctly.
    """
    if not prompt or not allowed_values:
        return []

    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {"type": "string", "enum": list(allowed_values)},
            }
        },
        "required": ["tags"],
    }
    sender_line = f"Avsender: {sender_role}\n\n" if sender_role else ""
    messages = [
        {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Tillatte tagger: {', '.join(allowed_values)}\n\n"
                f"{sender_line}"
                f"E-post:\n{prompt}"
            ),
        },
    ]

    start = time.monotonic()
    logger.info("LLM classify starting (input=%d chars)…", len(prompt))
    try:
        content, _ = await _chat_streaming(
            messages=messages,
            format_schema=schema,
            options={"temperature": 0.1},
            timeout_s=settings.ollama_timeout_s,
        )
    except Exception as err:
        logger.warning(
            "LLM classify call failed after %.1fs: %s: %s",
            time.monotonic() - start,
            type(err).__name__,
            err or "(no message)",
        )
        return []

    content = content.strip()
    if not content:
        logger.warning("LLM classify returned empty content")
        return []
    logger.info(
        "LLM classify done in %.1fs (output=%d chars)", time.monotonic() - start, len(content)
    )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("LLM classify returned non-JSON: %r", content[:300])
        return []

    raw_tags = parsed.get("tags") if isinstance(parsed, dict) else None
    if not isinstance(raw_tags, list):
        logger.warning("LLM classify JSON missing 'tags' list: %r", content[:300])
        return []

    allowed_set = set(allowed_values)
    # Preserve order, dedupe, drop anything outside the allowed set as a
    # final defense — `format=schema` should already enforce this server-side.
    seen: set[str] = set()
    out: list[str] = []
    for tag in raw_tags:
        if isinstance(tag, str) and tag in allowed_set and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out


# ============================================================
# classify_signature — locate signature lines (LLM picks WHICH line)
# ============================================================


@dataclass(frozen=True)
class SignatureLocators:
    """Verbatim signature lines the LLM located in the body.

    Each field is the line the LLM judged to CONTAIN that piece, returned
    EXACTLY as it appears (label and all) — not the extracted value.
    Deterministic cleaners in `utils/signature_parsing.py` turn each line into
    a stored value. `signature_first_line` is also a locator, used by the
    attachment/body-slicing code; it is consumed raw (uncleaned).

    Address is intentionally NOT located here: Norwegian addresses span two
    lines (street, then postal+city) and a single-line locator always loses
    half. The regex `parse_signature` handles addresses structurally instead.
    """

    title_line: str | None
    phone_line: str | None
    signature_first_line: str | None


_CLASSIFY_SIGNATURE_SYSTEM_PROMPT = (
    "You locate lines in a sender's email signature. Most senders are Norwegian "
    "(titles like 'Daglig leder', 'Digital rådgiver'), but English titles "
    "('CEO', 'Project Manager') appear too.\n\n"
    "WHAT A SIGNATURE IS:\n"
    "A signature is the block at the END of the sender's own message where "
    "they stop writing TO the reader and start stating WHO THEY ARE — their "
    "name, role/title, and contact details (phone, email, company, address, "
    "website). It MAY begin with a sign-off line ('Med vennlig hilsen', "
    "'Mvh', 'Best regards', 'Hilsen'), but VERY OFTEN there is NO sign-off "
    "at all — the message text just ends and the name line begins. The "
    "absence of a sign-off does NOT mean there is no signature. Labelled "
    "contact lines ('Tlf:', 'm:', 'Email:', a phone number, an email "
    "address) are strong signs you are inside a signature.\n\n"
    "WHERE THE SIGNATURE STARTS (this is `signature_first_line`):\n"
    "  - If there is a sign-off line, the signature starts at that sign-off "
    "line.\n"
    "  - If there is NO sign-off line, the signature starts at the sender's "
    "NAME line — the first line after the last sentence the sender wrote to "
    "the reader.\n"
    "Everything the sender wrote TO the reader (questions, statements, "
    "greetings like 'Hei Petter,', sign-offs that are part of the message "
    "like 'med vennlig hilsen' written mid-message) before the name is BODY.\n\n"
    "YOUR JOB: do NOT extract or reformat values. For each field, return the "
    "ONE existing line from the body that CONTAINS it, copied VERBATIM — "
    "preserve spaces, punctuation, capitalization, and any 'm:'/'Tlf:' "
    "label. Downstream code matches your string against the body lines and "
    "does the cleanup, so one extra or missing character breaks the match. "
    "Never combine two lines. Never invent a line that isn't present.\n\n"
    "Return JSON with exactly three fields (any may be null):\n"
    "  - title_line: the line containing the sender's JOB TITLE/role only "
    "('Daglig leder', 'Interiørarkitekt MA'). This is NOT the person's name, "
    "not a company, not a website, not an email. If the sender has no title "
    "line, return null — do NOT fall back to the name line.\n"
    "  - phone_line: the line containing the sender's phone number (keep the "
    "label, e.g. 'm: +47 …'). If several, the first.\n"
    "  - signature_first_line: the FIRST line of the signature block as "
    "defined above.\n\n"
    "Rules:\n"
    "  - Look at the sender's own signature only. Ignore quoted history, "
    "reply headers, and other people's signatures.\n"
    "  - If a field isn't present, return null for it.\n\n"
    "EXAMPLES:\n\n"
    "Example 1 — NO real sign-off ('med vennlig hilsen' is part of the "
    "message; the signature starts at the name line):\n"
    "Body:\n"
    "  Hei Petter,\n"
    "  Se bilde to 🙂\n"
    "  med vennlig hilsen\n"
    "  Caspar Vinje Hagland\n"
    "  Daglig leder| NIMREM\n"
    "  m: +47 93 88 32 01\n"
    "  e: cvh@nimrem.no\n"
    "  a: Parkveien 37 | NO0258 Oslo | Norway\n"
    "Answer:\n"
    '  {"title_line": "Daglig leder| NIMREM", '
    '"phone_line": "m: +47 93 88 32 01", '
    '"signature_first_line": "Caspar Vinje Hagland"}\n\n'
    "Example 2 — no title line (just name + phone). title_line is null — the "
    "name is NOT a title:\n"
    "Body:\n"
    "  Umiddelbart synes jeg dette ble bedre.\n"
    "  Charlotte Hagemoen\n"
    "  +4741682942\n"
    "Answer:\n"
    '  {"title_line": null, "phone_line": "+4741682942", '
    '"signature_first_line": "Charlotte Hagemoen"}\n\n'
    "Example 3 — no signature at all:\n"
    "Body:\n"
    "  Ok, høres bra ut. Snakkes!\n"
    "Answer:\n"
    '  {"title_line": null, "phone_line": null, '
    '"signature_first_line": null}'
)


async def classify_signature(
    body: str, sender_name: str | None = None
) -> SignatureLocators:
    """Locate the title / phone / first lines of a signature via the LLM.

    Best-effort: returns an all-None `SignatureLocators` on any failure
    (timeout, malformed output, etc.) so callers can degrade to the regex
    backstop gracefully.

    Each returned line is VERBATIM (label included) — the model picks WHICH
    line, deterministic cleaners turn it into a value. `signature_first_line`
    locates the signature region for attachment/body slicing.

    `sender_name` is passed to the model as a hint so it can disambiguate when
    the body contains multiple signatures (quoted history, forwards).
    """
    none_result = SignatureLocators(None, None, None)
    if not body or not body.strip():
        return none_result

    schema = {
        "type": "object",
        "properties": {
            "title_line": {"type": ["string", "null"]},
            "phone_line": {"type": ["string", "null"]},
            "signature_first_line": {"type": ["string", "null"]},
        },
        "required": ["title_line", "phone_line", "signature_first_line"],
    }
    sender_line = f"Sender name: {sender_name}\n\n" if sender_name else ""
    messages = [
        {"role": "system", "content": _CLASSIFY_SIGNATURE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"{sender_line}Email body:\n{body}",
        },
    ]

    start = time.monotonic()
    logger.info("LLM classify_signature starting (input=%d chars)…", len(body))
    try:
        content, _ = await _chat_streaming(
            messages=messages,
            format_schema=schema,
            options={"temperature": 0.0},
            timeout_s=settings.ollama_timeout_s,
        )
    except Exception as err:
        logger.warning(
            "LLM classify_signature failed after %.1fs: %s: %s",
            time.monotonic() - start,
            type(err).__name__,
            err or "(no message)",
        )
        return none_result

    content = content.strip()
    if not content:
        return none_result
    logger.info(
        "LLM classify_signature done in %.1fs (output=%d chars)",
        time.monotonic() - start,
        len(content),
    )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("LLM classify_signature returned non-JSON: %r", content[:300])
        return none_result
    if not isinstance(parsed, dict):
        return none_result

    def _clean(value: Any) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    return SignatureLocators(
        title_line=_clean(parsed.get("title_line")),
        phone_line=_clean(parsed.get("phone_line")),
        signature_first_line=_clean(parsed.get("signature_first_line")),
    )


# ============================================================
# split_history — fallback splitter for under-split forwarded chains
# ============================================================


_OSLO_TZ = timezone(timedelta(hours=1))


_SPLIT_HISTORY_SYSTEM_PROMPT = (
    "You split a forwarded/quoted email body into individual messages. The "
    "input is plain text that may contain MULTIPLE messages glued together — "
    "each separated by a reply/forward header in any format (Gmail-style "
    "'On X wrote:', Norwegian 'X skrev Y:', Outlook-style 'From: / Fra: / "
    "Sent: / Sendt: / Subject: / Emne:' blocks, possibly with markdown bold "
    "'*Fra:*' wrappers, '---------- Forwarded message ----------' dividers, "
    "etc.).\n\n"
    "Return EVERY distinct message you can identify, in chronological order "
    "(oldest first). For each message return:\n"
    "  - from: the author as 'Name <email>' or bare email or bare name — "
    "whatever the header carried.\n"
    "  - date_iso: ISO 8601 datetime ('2026-04-20T14:57:00+02:00'). If only "
    "a date is given, use 12:00 local time. If a timezone is missing, assume "
    "Europe/Oslo (+01:00 or +02:00 — pick +02:00 for summer dates, +01:00 "
    "for winter; if uncertain, use +01:00). If no date is recoverable, "
    "return an empty string for this field.\n"
    "  - subject: the subject line if present, else an empty string.\n"
    "  - body: the message's own content with the in-message reply headers "
    "and quoted history of OTHER messages REMOVED. Keep signatures intact. "
    "Use the original wording — do not paraphrase, translate, or summarize.\n"
    "  - raw_body: the verbatim span (including signature and any inline "
    "image markers like '[image: foo.png]') the message occupies in the "
    "input. This is used to attribute attachments — keep it faithful.\n\n"
    "Rules:\n"
    "  - DO NOT invent messages. If you can only confidently identify one "
    "message, return just that one.\n"
    "  - DO NOT translate or rewrite content. Preserve the original language "
    "(typically Norwegian).\n"
    "  - The OUTERMOST author (whose message contains all the others) goes "
    "LAST chronologically only if their date is newest — otherwise place "
    "everyone by their actual date.\n"
    "  - Return strictly valid JSON matching the schema."
)


def _parse_iso_dt(text: str, *, fallback: datetime) -> datetime:
    """Tolerant ISO 8601 parser. Falls back to `fallback` on any failure."""
    if not text:
        return fallback
    cleaned = text.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=fallback.tzinfo or _OSLO_TZ)
    return parsed


async def split_history(
    raw_body: str,
    *,
    parent_subject: str,
    parent_date: datetime,
) -> list[ExtractedMessage]:
    """LLM-driven fallback splitter for forwarded chains the regex layer
    under-splits.

    Returns ExtractedMessage records in chronological order (oldest first),
    matching the shape `extract_history_blocks` produces. On any failure
    (timeout, malformed JSON, empty result) returns [] so the caller can keep
    the regex result. This function is intentionally side-effect-free other
    than the LLM call.

    Cost: one Ollama call per under-split block. Caller decides when to
    invoke — typically only when `find_under_split_blocks` flagged the block.
    """
    if not raw_body or not raw_body.strip():
        return []

    schema = {
        "type": "object",
        "properties": {
            "messages": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "from": {"type": "string"},
                        "date_iso": {"type": "string"},
                        "subject": {"type": "string"},
                        "body": {"type": "string"},
                        "raw_body": {"type": "string"},
                    },
                    "required": ["from", "date_iso", "subject", "body", "raw_body"],
                },
            }
        },
        "required": ["messages"],
    }
    messages = [
        {"role": "system", "content": _SPLIT_HISTORY_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Parent subject: {parent_subject or '(none)'}\n\n"
                f"Email body to split:\n{raw_body}"
            ),
        },
    ]
    start = time.monotonic()
    logger.info("LLM split_history starting (input=%d chars)…", len(raw_body))
    try:
        content, _ = await _chat_streaming(
            messages=messages,
            format_schema=schema,
            options={"temperature": 0.0},
            timeout_s=settings.ollama_split_timeout_s,
        )
    except Exception as err:
        logger.warning(
            "LLM split_history call failed after %.1fs: %s: %s",
            time.monotonic() - start,
            type(err).__name__,
            err or "(no message)",
        )
        return []

    if not content.strip():
        logger.warning("LLM split_history returned empty content")
        return []
    logger.info(
        "LLM split_history done in %.1fs (output=%d chars)",
        time.monotonic() - start,
        len(content),
    )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        logger.warning("LLM split_history returned non-JSON: %r", content[:300])
        return []

    raw_messages = parsed.get("messages") if isinstance(parsed, dict) else None
    if not isinstance(raw_messages, list):
        return []

    out: list[ExtractedMessage] = []
    for entry in raw_messages:
        if not isinstance(entry, dict):
            continue
        from_field = str(entry.get("from") or "").strip()
        body_raw = str(entry.get("body") or "")
        raw_block = str(entry.get("raw_body") or "")
        # Run clean_body on the body the LLM gave us — same normalization
        # the regex path applies, so downstream consumers see a uniform shape.
        cleaned = clean_body(body_raw)
        if not from_field and not cleaned:
            continue
        # to_field/cc_field intentionally left empty here — the LLM fallback
        # doesn't currently extract recipient lists. ExtractedMessages from this
        # path won't populate the To/Cc relations on the Notion row, but the
        # From relation still works. The regex splitter (which handles the vast
        # majority of forwards) does populate To/Cc when the header carries them.
        out.append(
            ExtractedMessage(
                from_field=from_field,
                date=_parse_iso_dt(str(entry.get("date_iso") or ""), fallback=parent_date),
                subject=str(entry.get("subject") or "") or parent_subject,
                body=cleaned,
                raw_body=raw_block or body_raw,
            )
        )
    out.sort(key=lambda m: m.date)
    return out
