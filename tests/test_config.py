"""Tests for config.py's pyproject.toml version reader — pyproject.toml is
the single source of truth for the app's version number (surfaced in the
GUI header via /api/status), and this is a hand-rolled regex parse (not a
full TOML parser) so it needs its own coverage of the happy path and the
fallback-on-missing-file/malformed-line cases."""
from __future__ import annotations

import config


def test_read_pyproject_version_happy_path(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "stock-analyis"\nversion = "1.2.3"\n'
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert config._read_pyproject_version() == "1.2.3"


def test_read_pyproject_version_falls_back_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)  # no pyproject.toml written
    assert config._read_pyproject_version() == "unknown"


def test_read_pyproject_version_falls_back_when_version_line_absent(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "stock-analyis"\n')
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    assert config._read_pyproject_version() == "unknown"


def test_real_pyproject_version_is_readable():
    """Sanity check against the actual project file (not a fixture) — just
    confirms the regex matches this repo's real pyproject.toml format."""
    version = config._read_pyproject_version()
    assert version != "unknown"
    assert version == config.settings.app_version
