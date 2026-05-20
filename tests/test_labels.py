"""Tests for utils/labels.py — Gmail nested-label naming for Notion projects."""

from gb_automations.utils.labels import project_label_path, project_path_parts


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


# --- project_path_parts: shared source for Gmail label leaf AND NAS folder leaf ---


def test_path_parts_returns_year_and_leaf():
    assert project_path_parts("Acme", "2026-05-18T10:30:00.000Z") == ("2026", "Acme")


def test_path_parts_leaf_matches_label_leaf():
    # The NAS folder leaf MUST equal the Gmail label leaf — "the name is this
    # everywhere". Guard that parity so a future change to one can't silently
    # diverge from the other.
    title, created = "1187_Heimdal_Solsletta bygg D", "2026-01-01T00:00:00Z"
    year, leaf = project_path_parts(title, created)
    assert project_label_path(title, created) == f"Projects/{year}/{leaf}"


def test_path_parts_preserves_underscore_and_spaces():
    # Goldbox folder names use underscores and spaces; those are legal and must
    # survive sanitization unchanged.
    assert project_path_parts("1187_Heimdal_Solsletta bygg D", "2026-01-01T00:00:00Z") == (
        "2026",
        "1187_Heimdal_Solsletta bygg D",
    )


def test_path_parts_sanitizes_windows_illegal_chars():
    # Backslash, colon, etc. would make a Windows-share mkdir fail; all map to -.
    _, leaf = project_path_parts('a\\b:c*d?e"f<g>h|i', "2026-01-01T00:00:00Z")
    assert leaf == "a-b-c-d-e-f-g-h-i"
