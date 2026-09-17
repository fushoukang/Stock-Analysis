"""Tests for config.py's pyproject.toml version reader — pyproject.toml is
the single source of truth for the app's version number (surfaced in the
GUI header via /api/status), and this is a hand-rolled regex parse (not a
full TOML parser) so it needs its own coverage of the happy path and the
fallback-on-missing-file/malformed-line cases."""
from __future__ import annotations

import os

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


# --- registration_codes() / has_registration_code() ---
# A fresh Settings() instance is built per test rather than mutating the
# module-level `settings` singleton (frozen dataclass, and shared with
# every other test in this process) — registration_codes() re-reads
# os.environ at call time regardless of which instance it's called on.

def test_registration_codes_includes_legacy_and_group_specific(monkeypatch):
    monkeypatch.setenv("REGISTRATION_CODE", "general-code")
    monkeypatch.setenv("REGISTRATION_CODE_FAMILY", "family-code")
    monkeypatch.delenv("REGISTRATION_CODE_FRIENDS", raising=False)
    settings = config.Settings()
    codes = settings.registration_codes()
    assert codes["GENERAL"] == "general-code"
    assert codes["FAMILY"] == "family-code"
    assert settings.has_registration_code() is True


def test_registration_codes_group_names_are_uppercased(monkeypatch):
    monkeypatch.delenv("REGISTRATION_CODE", raising=False)
    # Isolate from any REGISTRATION_CODE_* that might already be sitting in
    # this process's real environment (not just .env) unrelated to this test.
    for key in list(os.environ):
        if key.startswith("REGISTRATION_CODE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REGISTRATION_CODE_friends", "fr-code")
    settings = config.Settings()
    assert settings.registration_codes() == {"FRIENDS": "fr-code"}


def test_registration_codes_empty_when_nothing_set(monkeypatch):
    monkeypatch.delenv("REGISTRATION_CODE", raising=False)
    for key in list(os.environ):
        if key.startswith("REGISTRATION_CODE_"):
            monkeypatch.delenv(key, raising=False)
    settings = config.Settings()
    assert settings.registration_codes() == {}
    assert settings.has_registration_code() is False


# --- admin_emails / is_admin() ---

def test_is_admin_matches_configured_email_case_insensitively(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Example.com, second@example.com")
    settings = config.Settings()
    assert settings.admin_emails == ["boss@example.com", "second@example.com"]
    assert settings.is_admin("boss@example.com") is True
    assert settings.is_admin("BOSS@EXAMPLE.COM") is True
    assert settings.is_admin("second@example.com") is True


def test_is_admin_false_for_unlisted_email(monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    settings = config.Settings()
    assert settings.is_admin("nobody@example.com") is False


def test_admin_emails_empty_by_default(monkeypatch):
    monkeypatch.delenv("ADMIN_EMAILS", raising=False)
    settings = config.Settings()
    assert settings.admin_emails == []
    assert settings.is_admin("anyone@example.com") is False
