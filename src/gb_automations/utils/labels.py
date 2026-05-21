"""Gmail label naming for Notion projects.

Single source of truth for the `Prosjekt/<year>/<project-name>` scheme.
Gmail uses `/` as the hierarchy separator in label names and auto-creates
parent labels when a nested name is sent to labels.create — no special API
needed. The top segment ("Prosjekt") comes from PROJECTS_LABEL_PREFIX.
"""
from __future__ import annotations

import logging

from gb_automations.config import PROJECTS_LABEL_PREFIX

logger = logging.getLogger(__name__)


def _year_from_created_time(created_time: str | None) -> str:
    # Notion returns ISO 8601 like "2026-05-18T10:30:00.000Z" — the first four
    # chars are the year. Validate they're digits so a malformed value doesn't
    # poison the label path.
    if created_time and len(created_time) >= 4 and created_time[:4].isdigit():
        return created_time[:4]
    logger.warning("missing/invalid created_time %r; using 'unknown' year", created_time)
    return "unknown"


# Characters that are illegal in Windows/SMB path components. A NAS folder leaf
# must avoid all of these; Gmail only cares about `/`, but we sanitize the same
# way everywhere so the project's leaf is byte-identical across label and folder
# (the team relies on "the name is this everywhere"). Goldbox's `_` and spaces
# are fine and deliberately preserved.
_ILLEGAL_LEAF_CHARS = '\\/:*?"<>|'


def _sanitize_leaf(name: str) -> str:
    # A literal `/` in the leaf would silently create an extra nesting level in
    # Gmail; the other chars would make the NAS mkdir fail on a Windows share.
    # Replace each with `-` so the project shows up as a single leaf everywhere.
    out = name
    for ch in _ILLEGAL_LEAF_CHARS:
        out = out.replace(ch, "-")
    return out.strip()


def project_path_parts(project_name: str, created_time: str | None) -> tuple[str, str]:
    """Return the `(year, leaf)` parts shared by the Gmail label and NAS folder.

    Single source of truth for the `<year>/<project-name>` scheme so the label
    leaf and the NAS folder leaf are guaranteed identical.

    >>> project_path_parts("Acme", "2026-05-18T10:30:00.000Z")
    ('2026', 'Acme')
    >>> project_path_parts("Foo/Bar", None)
    ('unknown', 'Foo-Bar')
    """
    return _year_from_created_time(created_time), _sanitize_leaf(project_name)


def project_label_path(project_name: str, created_time: str | None) -> str:
    """Return the full nested Gmail label name for a Notion project.

    >>> project_label_path("Acme", "2026-05-18T10:30:00.000Z")
    'Prosjekt/2026/Acme'
    >>> project_label_path("Foo/Bar", "2026-05-18T10:30:00.000Z")
    'Prosjekt/2026/Foo-Bar'
    >>> project_label_path("Acme", None)
    'Prosjekt/unknown/Acme'
    """
    year, leaf = project_path_parts(project_name, created_time)
    return f"{PROJECTS_LABEL_PREFIX}/{year}/{leaf}"
