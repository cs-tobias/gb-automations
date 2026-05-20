"""Tests for clients/nas.py — project folders on the office NAS share.

Runs entirely against a tmp_path root, so no real NAS is needed. We point
settings.nas_projects_root at the tmp dir via monkeypatch (the functions read
the module-level settings singleton at call time).
"""

from pathlib import Path

import pytest

from gb_automations.clients import nas
from gb_automations.config import settings

CREATED = "2026-05-18T10:30:00.000Z"


@pytest.fixture
def nas_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "nas_projects_root", str(tmp_path))
    monkeypatch.setattr(settings, "nas_received_subfolder", "Mottatt")
    return tmp_path


def test_project_dir_uses_year_and_leaf(nas_root: Path):
    assert nas.project_dir("1187_Heimdal", CREATED) == nas_root / "2026" / "1187_Heimdal"


def test_ensure_creates_project_and_received(nas_root: Path):
    target = nas.ensure_project_folders("1187_Heimdal", CREATED)
    assert target == nas_root / "2026" / "1187_Heimdal"
    assert target.is_dir()
    assert (target / "Mottatt").is_dir()


def test_ensure_is_idempotent(nas_root: Path):
    a = nas.ensure_project_folders("Acme", CREATED)
    b = nas.ensure_project_folders("Acme", CREATED)
    assert a == b and a.is_dir()


def test_ensure_heals_deleted_folder(nas_root: Path):
    target = nas.ensure_project_folders("Acme", CREATED)
    import shutil

    shutil.rmtree(target)
    assert not target.exists()
    healed = nas.ensure_project_folders("Acme", CREATED)
    assert healed.is_dir() and (healed / "Mottatt").is_dir()


def test_rename_moves_folder_in_place(nas_root: Path):
    old = nas.ensure_project_folders("Acme", CREATED)
    # Drop a file in Mottatt to prove the rename preserves contents.
    (old / "Mottatt" / "drawing.pdf").write_bytes(b"x")

    new = nas.rename_project_folder("Acme", "Acme Inc", CREATED)
    assert new == nas_root / "2026" / "Acme Inc"
    assert not old.exists()
    assert (new / "Mottatt" / "drawing.pdf").read_bytes() == b"x"


def test_rename_when_old_missing_creates_fresh(nas_root: Path):
    # Self-heal: no old folder (e.g. DB row exists but folder was deleted).
    new = nas.rename_project_folder("Ghost", "Renamed", CREATED)
    assert new == nas_root / "2026" / "Renamed"
    assert new.is_dir() and (new / "Mottatt").is_dir()


def test_rename_to_same_name_is_ensure(nas_root: Path):
    nas.ensure_project_folders("Acme", CREATED)
    result = nas.rename_project_folder("Acme", "Acme", CREATED)
    assert result == nas_root / "2026" / "Acme"
    assert result.is_dir()


def test_nas_available_true_for_writable_root(nas_root: Path):
    assert nas.nas_available() is True


def test_nas_available_false_when_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "nas_projects_root", "")
    assert nas.nas_available() is False


def test_nas_available_false_for_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "nas_projects_root", str(tmp_path / "nope"))
    assert nas.nas_available() is False
