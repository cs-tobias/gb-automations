"""Persistent installer state — `.setup_state.json` at repo root.

The state file remembers cross-step values (org id, project id, SA unique id,
Cloudflare zone id, tunnel id) so a re-run can resume without re-prompting.

Idempotency rule: real-world existence (does the SA exist? does the topic
exist?) is the source of truth — `.setup_state.json` is a cache of IDs we
discovered, not a record of "we did this step." If a step's resources already
exist when we check, we skip; if state mentions an ID that no longer exists,
we recreate.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_STATE_FILENAME = ".setup_state.json"


def _path(repo_root: Path) -> Path:
    return repo_root / _STATE_FILENAME


def load(repo_root: Path) -> dict[str, Any]:
    p = _path(repo_root)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # Corrupt file — back it up so the operator can inspect, start fresh.
        p.rename(p.with_suffix(".json.corrupt"))
        return {}


def save(repo_root: Path, state: dict[str, Any]) -> None:
    """Atomic write: tmp + rename, so a Ctrl-C mid-write never corrupts state."""
    p = _path(repo_root)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".setup_state.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp_path, p)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def update(repo_root: Path, **kwargs: Any) -> dict[str, Any]:
    """Read-modify-write for one or more keys."""
    state = load(repo_root)
    state.update(kwargs)
    save(repo_root, state)
    return state
