"""Tests for data/users.py — the salted-PBKDF2, email-keyed account store
behind manage_users.py, /signup, and web/auth.py. Each test points
settings.users_path at a throwaway file (tmp_path) so nothing here ever
touches the real users.json.

config.settings is a frozen dataclass, so it can't be mutated in place —
these tests rebind the module-level `settings` name inside data/users.py
itself (which is how monkeypatch.setattr works on plain module attributes)
rather than trying to patch a field on the real, frozen instance."""
from __future__ import annotations

from types import SimpleNamespace

from data import users as user_store


def _point_at_tmp_file(monkeypatch, tmp_path):
    monkeypatch.setattr(user_store, "settings", SimpleNamespace(users_path=str(tmp_path / "users.json")))


def test_add_and_verify_password_roundtrip(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("Fenix@Example.com", "correct horse battery staple")
    assert user_store.verify_password("fenix@example.com", "correct horse battery staple") is True


def test_verify_password_rejects_wrong_password(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "right-password")
    assert user_store.verify_password("fenix@example.com", "wrong-password") is False


def test_verify_password_rejects_unknown_user(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    assert user_store.verify_password("nobody@example.com", "anything") is False


def test_email_is_case_insensitive(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("Fenix@Example.com", "pw")
    assert user_store.user_exists("fenix@example.com") is True
    assert user_store.user_exists("FENIX@EXAMPLE.COM") is True
    assert [u.email for u in user_store.list_users()] == ["fenix@example.com"]


def test_add_user_rejects_duplicate(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw1")
    try:
        user_store.add_user("fenix@example.com", "pw2")
        assert False, "expected ValueError for a duplicate email"
    except ValueError:
        pass


def test_add_user_rejects_blank_password(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    try:
        user_store.add_user("fenix@example.com", "")
        assert False, "expected ValueError for a blank password"
    except ValueError:
        pass


def test_add_user_rejects_invalid_looking_emails(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    for bad_email in ["not-an-email", "missing-domain@", "@missing-local.com", "has space@example.com", "pipe|here@example.com"]:
        try:
            user_store.add_user(bad_email, "pw")
            assert False, f"expected ValueError for email {bad_email!r}"
        except ValueError:
            pass


def test_new_accounts_default_to_unverified(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw")
    assert user_store.is_verified("fenix@example.com") is False
    assert user_store.is_active("fenix@example.com") is False


def test_add_user_can_be_created_pre_verified(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw", verified=True)
    assert user_store.is_verified("fenix@example.com") is True
    assert user_store.is_active("fenix@example.com") is True


def test_mark_verified(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw")
    assert user_store.mark_verified("fenix@example.com") is True
    assert user_store.is_verified("fenix@example.com") is True
    # Idempotent — verifying an already-verified account is still a success.
    assert user_store.mark_verified("fenix@example.com") is True


def test_mark_verified_on_missing_user_returns_false(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    assert user_store.mark_verified("nobody@example.com") is False


def test_remove_user(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw")
    assert user_store.remove_user("fenix@example.com") is True
    assert user_store.user_exists("fenix@example.com") is False
    assert user_store.remove_user("fenix@example.com") is False  # already gone


def test_set_password_changes_credential(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "old-password")
    assert user_store.set_password("fenix@example.com", "new-password") is True
    assert user_store.verify_password("fenix@example.com", "old-password") is False
    assert user_store.verify_password("fenix@example.com", "new-password") is True


def test_set_password_on_missing_user_returns_false(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    assert user_store.set_password("nobody@example.com", "pw") is False


def test_passwords_are_never_stored_in_plaintext(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "super-secret-password")
    raw = (tmp_path / "users.json").read_text()
    assert "super-secret-password" not in raw


def test_load_users_survives_missing_file(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    assert user_store.load_users() == []
    assert user_store.list_users() == []


def test_load_users_survives_corrupt_file(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    (tmp_path / "users.json").write_text("not valid json{{{")
    assert user_store.load_users() == []


def test_add_user_stores_and_normalizes_group(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw", group="  family  ")
    users = user_store.list_users()
    assert len(users) == 1 and users[0].group == "FAMILY"


def test_add_user_defaults_to_no_group(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw")
    assert user_store.list_users()[0].group == ""


def test_group_survives_mark_verified_and_set_password(monkeypatch, tmp_path):
    _point_at_tmp_file(monkeypatch, tmp_path)
    user_store.add_user("fenix@example.com", "pw", group="friends")
    user_store.mark_verified("fenix@example.com")
    assert user_store.list_users()[0].group == "FRIENDS"
    user_store.set_password("fenix@example.com", "new-pw")
    assert user_store.list_users()[0].group == "FRIENDS"


def test_looks_like_email():
    assert user_store.looks_like_email("fenix@example.com") is True
    assert user_store.looks_like_email("not-an-email") is False
    assert user_store.looks_like_email("has space@example.com") is False
    assert user_store.looks_like_email("pipe|here@example.com") is False
