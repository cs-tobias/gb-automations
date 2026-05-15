"""Observability helpers — request-scoped IDs that follow a webhook through its call tree.

Every incoming webhook starts a `request_scope("evt")` block. While that block
is active, all `logger.*` calls anywhere in the process (sync_thread, llm,
notion client, gmail client, …) get a `[<prefix>:<id>]` tag prefixed to the
message via `RequestIdFilter`. After the block exits the tag goes away.

Why a contextvar and not threading.local: FastAPI runs handlers on the
asyncio event loop. `contextvars` is the only mechanism that survives across
`await` boundaries inside the same task, and is copied when a task spawns a
child task (matters for our `asyncio.create_task(_run_thread_syncs_background(...))`
pattern in webhooks.py — the background task inherits the parent's ID).
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
