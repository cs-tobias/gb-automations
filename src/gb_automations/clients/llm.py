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
from typing import Any

import httpx

from gb_automations.config import settings

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


_CLASSIFY_SYSTEM_PROMPT = (
    "Du klassifiserer kroppen til en e-post fra et 3D-rendering-studio "
    "(Goldbox). Du får KUN selve meldingsteksten — ikke emnefelt, ikke "
    "tråd-kontekst.\n\n"
    "KONTEKST: Goldbox er studio-teamet. Andre avsendere er klienter eller "
    "samarbeidspartnere som studioet jobber for. 'Avsender:' i input forteller "
    "deg om denne e-posten er skrevet av en intern (Goldbox) eller ekstern "
    "(klient) person.\n\n"
    "Tagene har to akser:\n"
    "  1. Kommunikasjons-type — hva slags e-post er dette? "
    "(tilbud, bestilling, korreksjon, leveranse, underlag, spørsmål, møte, faktura, intern)\n"
    "  2. Tema/innhold — hva handler e-posten OM? "
    "(kjøkken, bad, stue, soverom, inngangsparti, fasade, korridor, balkong, "
    "utomhus, plantegning, detalj, farger)\n\n"
    "RETNING (viktig — flere tagger er retnings-avhengige):\n"
    "  • 'leveranse' = Goldbox LEVERER til klient (bilder, renders, ferdige "
    "filer sendes UT). Brukes når avsender er INTERN og e-posten kunngjør at "
    "noe er klart/sendt til klienten. En vag intensjon om å levere senere "
    "('vi sikter på mandag', 'kommer tilbake i morgen') er IKKE leveranse — "
    "det er bare en status-oppdatering.\n"
    "  • 'underlag' = klient SENDER inn brief/spec/referansemateriale TIL "
    "Goldbox (plantegninger, moodboards, WeTransfer med dokumenter). Brukes "
    "når avsender er EKSTERN og sender materiale Goldbox skal jobbe ut fra.\n"
    "  • 'korreksjon' = en konkret tilbakemelding/endring som skal gjøres på "
    "rendringen. Klient gir korreksjon ('vi vil ha andre planter i hjørnet', "
    "'fargen er for mørk', 'kommentarer er lagt inn i Frame'). Goldbox kan "
    "også 'ha gjort' korreksjoner. Men en INTERN avsender som ber klienten "
    "om å kommentere ('Kommenter gjerne på vinkelen') er IKKE korreksjon — "
    "det er bare en forespørsel om innspill.\n"
    "  • 'møte' = en faktisk avtale, samtale eller møte er planlagt, bekreftet "
    "eller referert (Teams/Zoom-lenke, tidspunkt, sted, 'snakkes kl 14', "
    "'møtes i morgen'). Generelle spørsmål om status ('hvordan ligger vi an?', "
    "'noe nytt?') er IKKE møte.\n"
    "  • 'bestilling' = klient godkjenner et tilbud eller bestiller en konkret "
    "leveranse ('vi godkjenner tilbudet', 'bestiller pakken'). Generelle "
    "positive svar ('høres bra ut', 'ligger godt an') er IKKE bestilling.\n\n"
    "REGLER (viktige):\n"
    "  • Vær STRENG. Bedre å returnere FÅ eller INGEN tagger enn å gjette.\n"
    "  • Hvis du er i TVIL om en tag passer skikkelig, IKKE bruk den. Tom "
    "liste {\"tags\": []} er det riktige svaret for status-sjekker, vage "
    "intensjoner og generelle hilsener uten konkret handling.\n"
    "  • En tag krever at teksten du leser eksplisitt diskuterer det. Ikke "
    "anta noe ut fra prosjekt-kontekst du ikke ser.\n"
    "  • Korte hilsener/bekreftelser ('Tusen takk', 'Supert', 'OK', 'Snakkes "
    "straks') skal IKKE få tema-tagger. Vurder kun en kommunikasjons-tag "
    "(f.eks. 'møte' for 'snakkes straks på Teams') eller returner tom liste.\n"
    "  • Hvis ingenting passer skikkelig, returner en TOM liste: {\"tags\": []}. "
    "Det er et helt gyldig svar.\n"
    "  • 'annet' bruker du kun hvis e-posten har et tydelig formål men ingen "
    "av de andre kategoriene passer.\n\n"
    "EKSEMPLER:\n"
    "  Avsender: intern (Goldbox)\n"
    "  Tekst: 'Så bra! Kommenter gjerne på den nye vinkelen ☺️'\n"
    "  Tags:  []  (Goldbox ber klienten om innspill — ikke en korreksjon, "
    "ingen tema nevnt)\n\n"
    "  Avsender: ekstern (klient)\n"
    "  Tekst: 'Vi liker ikke plantene i hjørnet av inngangspartiet, kan de byttes ut?'\n"
    "  Tags:  ['korreksjon', 'inngangsparti']  (konkret endrings-ønske fra klient)\n\n"
    "  Avsender: ekstern (klient)\n"
    "  Tekst: 'Hei, sender over en wetransfer med plantegninger og referansebilder.'\n"
    "  Tags:  ['underlag']  (klient sender brief-materiale inn til Goldbox)\n\n"
    "  Avsender: intern (Goldbox)\n"
    "  Tekst: 'Da er bildene rendret og lastet opp på Frame. Klar for gjennomgang.'\n"
    "  Tags:  ['leveranse']  (Goldbox leverer ferdige filer ut til klient)\n\n"
    "  Avsender: ekstern (klient)\n"
    "  Tekst: 'Da har vi lagt inn alle kommentarer på inngangspartiet ☺️'\n"
    "  Tags:  ['korreksjon', 'inngangsparti']  (klient har gitt korreksjoner)\n\n"
    "  Tekst: 'Tak, snakkes straks ☺️'\n"
    "  Tags:  ['møte']\n\n"
    "  Tekst: 'Tusen takk Heidi ☺️'\n"
    "  Tags:  []\n\n"
    "  Avsender: ekstern (klient)\n"
    "  Tekst: 'Hei, takk for tilbudet på kjøkkenet. Vi godkjenner.'\n"
    "  Tags:  ['bestilling', 'kjøkken']\n\n"
    "  Tekst: 'Ny mail, hvordan ligger vi an?'\n"
    "  Tags:  []  (status-sjekk, ikke et møte og ikke et tema)\n\n"
    "  Avsender: intern (Goldbox)\n"
    "  Tekst: 'Ligger godt an, ser for oss å levere mandag 18. mai'\n"
    "  Tags:  []  (vag intensjon om leveranse — ikke konkret leveranse, ikke "
    "bestilling, bare en status-oppdatering)\n\n"
    "Returner KUN et JSON-objekt med nøkkelen 'tags' og en liste av strenger "
    "(eventuelt tom). Bruk kun tagene fra det tillatte settet."
)


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
