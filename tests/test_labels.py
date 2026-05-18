"""Tests for utils/labels.py — Gmail nested-label naming for Notion projects."""

from gb_automations.utils.labels import project_label_path


def test_normal_case():
    assert project_label_path("Acme", "2026-05-18T10:30:00.000Z") == "Projects/2026/Acme"


def test_year_taken_from_first_four_chars():
    assert project_label_path("Foo", "2024-01-01T00:00:00Z") == "Projects/2024/Foo"


def test_slash_in_name_sanitized_to_dash():
    assert project_label_path("Foo/Bar", "2026-01-01T00:00:00Z") == "Projects/2026/Foo-Bar"


def test_missing_created_time_uses_unknown_year():
    assert project_label_path("Acme", None) == "Projects/unknown/Acme"


def test_empty_created_time_uses_unknown_year():
    assert project_label_path("Acme", "") == "Projects/unknown/Acme"


def test_non_iso_created_time_uses_unknown_year():
    assert project_label_path("Acme", "garbage") == "Projects/unknown/Acme"


def test_whitespace_in_leaf_trimmed():
    assert project_label_path("  Acme  ", "2026-01-01T00:00:00Z") == "Projects/2026/Acme"


def test_multiple_slashes_all_sanitized():
    assert project_label_path("a/b/c", "2026-01-01T00:00:00Z") == "Projects/2026/a-b-c"
