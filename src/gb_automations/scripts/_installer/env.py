"""Read/write `.env` preserving comments, blank lines, and key ordering.

The repo's `.env.example` carries multi-paragraph comment blocks that explain
WHY each setting exists — losing them when the installer writes a value would
make the resulting `.env` an unreadable wall of `KEY=value`. So this module
edits in place: for each `KEY=...` line, replace the value; for new keys,
append at the bottom under a "# Added by installer" header.
"""

from __future__ import annotations

import re
from pathlib import Path

_KEY_RE = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")


def load(env_path: Path) -> dict[str, str]:
    """Return a key -> value mapping. Ignores comments and blank lines."""
    if not env_path.exists():
        return {}
    out: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _KEY_RE.match(stripped)
        if m:
            out[m.group(1)] = _unquote(m.group(2))
    return out


def set_values(env_path: Path, updates: dict[str, str | None]) -> None:
    """Update keys in place; append new ones at the end.

    A value of None deletes the key entirely (so we don't leave a dangling
    `FOO=` line when we want to roll back).
    """
    if not env_path.exists():
        # Bootstrap from .env.example if a fresh repo has no .env yet.
        example = env_path.parent / ".env.example"
        if example.exists():
            env_path.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            env_path.write_text("", encoding="utf-8")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    seen: set[str] = set()
    out_lines: list[str] = []

    for line in lines:
        m = _KEY_RE.match(line.strip())
        if m and m.group(1) in updates:
            key = m.group(1)
            seen.add(key)
            new_value = updates[key]
            if new_value is None:
                # Drop the line — caller asked for deletion.
                continue
            out_lines.append(f"{key}={_quote_if_needed(new_value)}")
        else:
            out_lines.append(line)

    new_keys = [k for k, v in updates.items() if k not in seen and v is not None]
    if new_keys:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# Added by installer")
        for k in new_keys:
            out_lines.append(f"{k}={_quote_if_needed(updates[k] or '')}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _quote_if_needed(value: str) -> str:
    # Quote only when whitespace or `#` would otherwise break parsing.
    if any(c in value for c in " \t#"):
        escaped = value.replace('"', '\\"')
        return f'"{escaped}"'
    return value
