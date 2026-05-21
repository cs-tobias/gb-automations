"""Observability helpers — request-scoped IDs that follow a webhook through its call tree.

Every incoming webhook starts a `request_scope("evt")` block. While that block
is active, all `logger.*` calls anywhere in the process (sync_thread, llm,
notion client, gmail client, …) get a `[<prefix>:<id>]` tag prefixed to the
message via `RequestIdFilter`. After the block exits the tag goes away.

Why a contextvar and not threading.local: FastAPI runs handlers on the
asyncio event loop. `contextvars` is the only mechanism that survives across
`await` boundaries inside the same task, and is copied when a task spawns a
child task. Note the Gmail sync no longer runs inside the request task — the
webhook only enqueues a durable `sync_tasks` row and the queue worker (a
long-lived task started in lifespan) does the sync later, so worker log lines
carry the worker's own scope, not the originating webhook's.
"""

from __future__ import annotations

import logging
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

# 4 hex chars = ~65k IDs before collision; collisions only matter within a few
# seconds of overlapping logs, so this is plenty.
_REQUEST_ID: ContextVar[str] = ContextVar("gb_request_id", default="")


def current_request_id() -> str:
    """Return the active request ID, or empty string if no scope is open."""
    return _REQUEST_ID.get()


@contextmanager
def request_scope(prefix: str = "req") -> Iterator[str]:
    """Open a request-scoped logging context. Returns the generated ID.

    Usage:
        with request_scope("gmail") as rid:
            logger.info("starting")  # → "[gmail:a3f2] starting"

    Nested calls are allowed but rare — the inner scope replaces the outer for
    its duration. Child asyncio tasks spawned inside the scope inherit the ID.
    """
    rid = f"{prefix}:{secrets.token_hex(2)}"
    token = _REQUEST_ID.set(rid)
    try:
        yield rid
    finally:
        _REQUEST_ID.reset(token)


class RequestIdFilter(logging.Filter):
    """Stamps every LogRecord with `record.request_id` so the formatter can include it.

    Records emitted outside any `request_scope` get an empty string — the
    formatter renders this as just whitespace via the `%(request_id)s` slot.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        rid = _REQUEST_ID.get()
        # Pad to a fixed width so log columns stay aligned whether or not a
        # request is in flight. " " * 12 matches "[gmail:abcd] " width.
        record.request_id = f"[{rid}] " if rid else ""
        return True


# ============================================================
# Human-readable colored console formatter
# ============================================================

import os  # noqa: E402 — kept local to the formatter section

# ANSI SGR codes. These render identically inside the Linux container regardless
# of host OS; the host terminal showing `docker compose logs` (macOS Terminal/
# iTerm, Windows Terminal, PowerShell 7) all interpret them. We auto-disable when
# output isn't a TTY (piped to a file/CI) or when NO_COLOR is set.
_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"

_LEVEL_COLORS = {
    "DEBUG": "\033[36m",    # cyan
    "INFO": "\033[32m",     # green
    "WARNING": "\033[33m",  # yellow
    "ERROR": "\033[31m",    # red
    "CRITICAL": "\033[1;41m",  # bold white on red
}
# Abbreviate the level to a tidy fixed 5-col width.
_LEVEL_LABELS = {
    "DEBUG": "DEBUG",
    "INFO": "INFO ",
    "WARNING": "WARN ",
    "ERROR": "ERROR",
    "CRITICAL": "CRIT ",
}
# Stable palette for the [prefix:id] request tag, so one webhook's whole call
# tree shares a color and is easy to track in interleaved output.
_TAG_COLORS = (
    "\033[35m",  # magenta
    "\033[34m",  # blue
    "\033[36m",  # cyan
    "\033[95m",  # bright magenta
    "\033[94m",  # bright blue
    "\033[96m",  # bright cyan
)


def _color_enabled() -> bool:
    """Decide whether to emit ANSI color. LOG_COLOR overrides; else auto-detect."""
    override = os.environ.get("LOG_COLOR")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    if os.environ.get("NO_COLOR") is not None:
        return False
    # Docker keeps stdout a pipe, not a TTY, but `docker compose logs` still
    # renders ANSI — so we default ON unless explicitly disabled. Operators who
    # pipe to a file can set NO_COLOR=1 or LOG_COLOR=0.
    return True


class ColorFormatter(logging.Formatter):
    """Compact, colorized one-line console format.

    Layout:  HH:MM:SS  LEVEL  component  [tag] message
      - timestamp dimmed, level color-coded,
      - `component` is the logger name's last segment (gb_automations.sync.
        sync_thread → sync_thread), left-padded to a fixed width for alignment,
      - the request tag (if any) colored by a hash of its prefix:id.

    Falls back to plain text (no codes) when color is disabled.
    """

    _COMPONENT_WIDTH = 16

    def __init__(self) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self._color = _color_enabled()

    def _paint(self, text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if self._color else text

    def _tag_color(self, tag: str) -> str:
        return _TAG_COLORS[hash(tag) % len(_TAG_COLORS)]

    def format(self, record: logging.LogRecord) -> str:
        ts = self._paint(self.formatTime(record, self.datefmt), _DIM)

        raw_level = record.levelname
        level = self._paint(
            _LEVEL_LABELS.get(raw_level, raw_level[:5].ljust(5)),
            _LEVEL_COLORS.get(raw_level, ""),
        )

        # Show the logger's last segment, except collapse uvicorn's sub-loggers
        # (uvicorn.error / uvicorn.access) to just "uvicorn" — "error" as a
        # component name is misleading.
        name = record.name
        comp = "uvicorn" if name.startswith("uvicorn") else name.rsplit(".", 1)[-1]
        component = self._paint(comp[: self._COMPONENT_WIDTH].ljust(self._COMPONENT_WIDTH), _DIM)

        # request_id is set by RequestIdFilter as "[gmail:abcd] " or "".
        tag = getattr(record, "request_id", "")
        if tag and self._color:
            tag = f"{self._tag_color(tag)}{tag}{_RESET}"

        message = record.getMessage()
        line = f"{ts} {level} {component} {tag}{message}"

        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def describe_error(err: Exception) -> str:
    """A short, never-empty description of an exception for one-line logs.

    Many transient client errors (httpx ReadTimeout/ConnectError) stringify to
    "" — so we fall back to the exception class name. Known API errors expose a
    concise `summary` we prefer.
    """
    summary = getattr(err, "summary", None)
    if summary:
        return str(summary)
    text = str(err).strip()
    return text or type(err).__name__


# Expected-transient exception class names we one-line rather than dump a full
# traceback for. Matched by name so we don't import httpx here.
_EXPECTED_TRANSIENT = {
    "ReadTimeout",
    "ConnectTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "TimeoutException",
    "ConnectError",
    "ReadError",
    "RemoteProtocolError",
}


def log_api_error(logger: logging.Logger, context: str, err: Exception) -> None:
    """Log an external-API error as ONE clean line, full detail at DEBUG only.

    Known API errors (NotionAPIError) and expected-transient network errors
    (httpx timeouts/connection drops) are logged as a single WARNING line with
    the full traceback demoted to DEBUG. Genuinely-unknown exceptions still get
    a full traceback so we never hide a real bug.
    """
    is_known = getattr(err, "summary", None) is not None
    is_transient = type(err).__name__ in _EXPECTED_TRANSIENT
    if is_known or is_transient:
        logger.warning("%s: %s", context, describe_error(err))
        detail = getattr(err, "detail", None)
        if detail:
            logger.debug("%s (full): %s", context, detail)
        else:  # keep the traceback reachable at DEBUG for transient errors
            logger.debug("%s (full)", context, exc_info=err)
    else:
        logger.exception("%s", context)
