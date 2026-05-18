"""Gmail label naming for Notion projects.

Single source of truth for the `Projects/<year>/<project-name>` scheme.
Gmail uses `/` as the hierarchy separator in label names and auto-creates
parent labels when a nested name is sent to labels.create — no special API
needed.
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


def _sanitize_leaf(name: str) -> str:
    # A literal `/` in the leaf would silently create an extra nesting level
    # in Gmail. Replace with `-` so the project shows up as a single leaf.
    return name.replace("/", "-").strip()


def project_label_path(project_name: str, created_time: str | None) -> str:
    """Return the full nested Gmail label name for a Notion project.

    >>> project_label_path("Acme", "2026-05-18T10:30:00.000Z")
    'Projects/2026/Acme'
    >>> project_label_path("Foo/Bar", "2026-05-18T10:30:00.000Z")
    'Projects/2026/Foo-Bar'
    >>> project_label_path("Acme", None)
    'Projects/unknown/Acme'
    """
    year = _year_from_created_time(created_time)
    leaf = _sanitize_leaf(project_name)
    return f"{PROJECTS_LABEL_PREFIX}/{year}/{leaf}"
