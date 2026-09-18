"""Tests for web/auth.py — the signed-cookie session tokens, the separately
purposed email-verification tokens, credential check, per-IP rate limiting,
and the open-redirect guard behind the web GUI's login accounts (see
web/app.py's require_login middleware and /login, /signup, /verify,
/resend-verification routes for how these get used).

Two things get faked out via monkeypatch, since both are plain module-level
names inside web/auth.py rather than something instantiable per-test:
  - `auth.settings` (config.settings is a frozen dataclass — can't mutate a
    field on the real instance, so the name itself gets rebound instead)
  - `auth.user_store` (a stand-in for data/users.py, so these tests aren't
    also exercising the real PBKDF2 hashing/JSON file — that's covered by
    tests/test_users.py on its own)"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from web import auth


def _fake_settings(**overrides):
    base = dict(
        session_secret_key="test-secret-key",
        session_max_age_hours=1,
        verification_token_max_age_hours=1,
        registration_code="",
        registration_codes=lambda: {},
        has_registration_code=lambda: False,
        has_smtp_credentials=lambda: False,
        is_admin=lambda email: False,
        reset_token_max_age_hours=1,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _fake_user_store(accounts: dict[str, dict] | None = None):
    """`accounts` maps normalized email -> {"password": ..., "verified": ...}."""
    accounts = accounts or {}

    def normalize_email(email):
        return email.strip().lower()

    def verify_password(email, password):
        acct = accounts.get(normalize_email(email))
        return acct is not None and acct["password"] == password

    def user_exists(email):
        return normalize_email(email) in accounts

    def is_verified(email):
        acct = accounts.get(normalize_email(email))
        return bool(acct and acct.get("verified"))

    def is_active(email):
        return is_verified(email)

    def load_users():
        return [SimpleNamespace(email=e) for e in accounts]

    def looks_like_email(email):
        return "@" in email and "." in email.split("@")[-1] and " " not in email

    return SimpleNamespace(
        normalize_email=normalize_email,
        verify_password=verify_password,
        user_exists=user_exists,
        is_verified=is_verified,
        is_active=is_active,
        load_users=load_users,
        looks_like_email=looks_like_email,
    )


@pytest.fixture(autouse=True)
def _isolated_rate_limit_state(monkeypatch):
    """Every test gets its own empty attempts dict, so failures recorded in
    one test can't make an unrelated test see a false rate-limit hit."""
    monkeypatch.setattr(auth, "_failed_attempts", {})


# --- Session tokens ---

def test_session_token_roundtrip(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("fenix@example.com")
    assert auth.verify_session_token(token) == "fenix@example.com"


def test_session_token_rejects_unverified_account(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    token = auth.create_session_token("fenix@example.com")
    assert auth.verify_session_token(token) is None


def test_session_token_rejects_if_account_removed(monkeypatch):
    # Simulates `manage_users.py remove` happening while someone's already
    # logged in — their existing cookie must stop working immediately.
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("fenix@example.com")
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))  # account removed
    assert auth.verify_session_token(token) is None


def test_session_token_rejects_tampered_signature(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    payload, _, signature = auth.create_session_token("fenix@example.com").rpartition(".")
    flipped = ("0" if signature[0] != "0" else "1") + signature[1:]
    assert auth.verify_session_token(f"{payload}.{flipped}") is None


def test_session_token_rejects_expired(monkeypatch):
    # A negative max-age puts expires_at in the past at creation time, no
    # need to fake the clock.
    monkeypatch.setattr(auth, "settings", _fake_settings(session_max_age_hours=-1))
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("fenix@example.com")
    assert auth.verify_session_token(token) is None


@pytest.mark.parametrize(
    "bad_token",
    [None, "", "no-dot-here", "abc.def", "session|fenix@example.com.deadbeef", "session|fenix@example.com|notanumber.deadbeef"],
)
def test_session_token_rejects_malformed(monkeypatch, bad_token):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    assert auth.verify_session_token(bad_token) is None


def test_session_token_signed_with_different_secret_is_rejected(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings(session_secret_key="key-one"))
    token = auth.create_session_token("fenix@example.com")
    monkeypatch.setattr(auth, "settings", _fake_settings(session_secret_key="key-two"))
    assert auth.verify_session_token(token) is None


def test_secret_key_is_generated_and_cached_when_unset(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(session_secret_key=""))
    monkeypatch.setattr(auth, "_generated_secret", None)
    monkeypatch.setattr(auth, "_warned_no_secret", False)
    first = auth._secret_key()
    second = auth._secret_key()
    assert first == second
    assert isinstance(first, bytes) and len(first) == 32


def test_get_current_email_and_is_authenticated_from_a_cookie(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("fenix@example.com")
    conn = SimpleNamespace(cookies={auth.SESSION_COOKIE_NAME: token})
    assert auth.get_current_email(conn) == "fenix@example.com"
    assert auth.is_authenticated(conn) is True

    conn_no_cookie = SimpleNamespace(cookies={})
    assert auth.get_current_email(conn_no_cookie) is None
    assert auth.is_authenticated(conn_no_cookie) is False


# --- Email-verification tokens (a distinct "purpose" from session tokens) ---

def test_verification_token_roundtrip(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    token = auth.create_verification_token("fenix@example.com")
    assert auth.verify_verification_token(token) == "fenix@example.com"


def test_verification_token_rejects_expired(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(verification_token_max_age_hours=-1))
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    token = auth.create_verification_token("fenix@example.com")
    assert auth.verify_verification_token(token) is None


def test_verification_token_rejects_unknown_account(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    token = auth.create_verification_token("nobody@example.com")
    assert auth.verify_verification_token(token) is None


def test_session_token_cannot_be_replayed_as_a_verification_token(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    session_token = auth.create_session_token("fenix@example.com")
    assert auth.verify_verification_token(session_token) is None


def test_verification_token_cannot_be_replayed_as_a_session_token(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    verify_token = auth.create_verification_token("fenix@example.com")
    assert auth.verify_session_token(verify_token) is None


# --- Password reset tokens (a third distinct "purpose") ---

def test_reset_token_roundtrip(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_reset_token("fenix@example.com")
    assert auth.verify_reset_token(token) == "fenix@example.com"


def test_reset_token_rejects_expired(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(reset_token_max_age_hours=-1))
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_reset_token("fenix@example.com")
    assert auth.verify_reset_token(token) is None


def test_reset_token_rejects_unknown_account(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    token = auth.create_reset_token("nobody@example.com")
    assert auth.verify_reset_token(token) is None


def test_reset_token_works_for_an_unverified_account(monkeypatch):
    # Password reset doesn't require prior verification — the account
    # simply gets verified as a side effect once the reset is submitted
    # (see web/app.py's /reset-password), not by the token itself.
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    token = auth.create_reset_token("fenix@example.com")
    assert auth.verify_reset_token(token) == "fenix@example.com"


def test_reset_token_cannot_be_replayed_as_a_session_token(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    reset_token = auth.create_reset_token("fenix@example.com")
    assert auth.verify_session_token(reset_token) is None


def test_reset_token_cannot_be_replayed_as_a_verification_token(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    reset_token = auth.create_reset_token("fenix@example.com")
    assert auth.verify_verification_token(reset_token) is None


def test_verification_token_cannot_be_replayed_as_a_reset_token(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    verify_token = auth.create_verification_token("fenix@example.com")
    assert auth.verify_reset_token(verify_token) is None


# --- Credential check ---

def test_check_credentials_correct(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "hunter2", "verified": True}}))
    assert auth.check_credentials("fenix@example.com", "hunter2") == "fenix@example.com"


def test_check_credentials_is_case_insensitive_on_email(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "hunter2", "verified": True}}))
    assert auth.check_credentials("Fenix@Example.com", "hunter2") == "fenix@example.com"


@pytest.mark.parametrize("email,password", [("fenix@example.com", "wrong"), ("nobody@example.com", "hunter2")])
def test_check_credentials_incorrect(monkeypatch, email, password):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "hunter2", "verified": True}}))
    assert auth.check_credentials(email, password) is None


def test_check_credentials_fails_closed_when_no_accounts_exist(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    assert auth.check_credentials("anyone@example.com", "anything") is None


def test_check_credentials_succeeds_even_when_unverified(monkeypatch):
    # By design: web/app.py's /login checks is_verified() separately so it
    # can show "please verify your email" instead of a generic wrong-
    # password error — check_credentials itself is password-only.
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "hunter2", "verified": False}}))
    assert auth.check_credentials("fenix@example.com", "hunter2") == "fenix@example.com"


def test_has_any_users(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    assert auth.has_any_users() is False
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    assert auth.has_any_users() is True


def test_registration_open_requires_both_code_and_smtp(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: False, has_smtp_credentials=lambda: False))
    assert auth.registration_open() is False
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: True, has_smtp_credentials=lambda: False))
    assert auth.registration_open() is False
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: False, has_smtp_credentials=lambda: True))
    assert auth.registration_open() is False
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: True, has_smtp_credentials=lambda: True))
    assert auth.registration_open() is True


# --- Admin gate ---

def test_is_admin_true_for_admin_email(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(is_admin=lambda email: email == "boss@example.com"))
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"boss@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("boss@example.com")
    conn = SimpleNamespace(cookies={auth.SESSION_COOKIE_NAME: token})
    assert auth.is_admin(conn) is True


def test_is_admin_false_for_non_admin_email(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(is_admin=lambda email: email == "boss@example.com"))
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    token = auth.create_session_token("fenix@example.com")
    conn = SimpleNamespace(cookies={auth.SESSION_COOKIE_NAME: token})
    assert auth.is_admin(conn) is False


def test_is_admin_false_when_not_logged_in(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(is_admin=lambda email: True))
    conn = SimpleNamespace(cookies={})
    assert auth.is_admin(conn) is False


# --- Registration code -> group lookup ---

def test_group_for_code_matches_a_configured_code(monkeypatch):
    monkeypatch.setattr(
        auth, "settings", _fake_settings(registration_codes=lambda: {"general": "let-me-in"})
    )
    assert auth.group_for_code("let-me-in") == "general"


def test_group_for_code_distinguishes_multiple_groups(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _fake_settings(
            registration_codes=lambda: {"family": "fam-code", "friends": "fr-code"}
        ),
    )
    assert auth.group_for_code("fam-code") == "family"
    assert auth.group_for_code("fr-code") == "friends"


def test_group_for_code_returns_none_for_unknown_code(monkeypatch):
    monkeypatch.setattr(
        auth, "settings", _fake_settings(registration_codes=lambda: {"general": "let-me-in"})
    )
    assert auth.group_for_code("wrong-code") is None


def test_group_for_code_returns_none_when_no_codes_configured(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(registration_codes=lambda: {}))
    assert auth.group_for_code("anything") is None


# --- Rate limiting ---

def test_rate_limiting_locks_out_after_max_attempts():
    key = "login:192.0.2.1"
    for _ in range(auth._MAX_ATTEMPTS):
        assert auth.is_rate_limited(key) is False
        auth.record_failed_attempt(key)
    assert auth.is_rate_limited(key) is True


def test_rate_limiting_is_per_key():
    for _ in range(auth._MAX_ATTEMPTS):
        auth.record_failed_attempt("login:192.0.2.1")
    assert auth.is_rate_limited("login:192.0.2.1") is True
    # Different action, same IP — must not share the same bucket.
    assert auth.is_rate_limited("signup:192.0.2.1") is False
    # Same action, different IP.
    assert auth.is_rate_limited("login:192.0.2.2") is False


def test_reset_attempts_clears_lockout():
    key = "login:192.0.2.1"
    for _ in range(auth._MAX_ATTEMPTS):
        auth.record_failed_attempt(key)
    assert auth.is_rate_limited(key) is True
    auth.reset_attempts(key)
    assert auth.is_rate_limited(key) is False


# --- Open-redirect guard ---

@pytest.mark.parametrize("path", ["/", "/login", "/api/status", "/some/deep/path"])
def test_is_safe_redirect_path_accepts_relative_paths(path):
    assert auth.is_safe_redirect_path(path) is True


@pytest.mark.parametrize(
    "path",
    ["", "foo", "//evil.com", "//evil.com/x", "http://evil.com", "https://evil.com/x", "/x://y"],
)
def test_is_safe_redirect_path_rejects_unsafe_paths(path):
    assert auth.is_safe_redirect_path(path) is False


# --- Login page rendering ---

def test_render_login_page_shows_not_configured_notice(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    monkeypatch.setattr(auth, "settings", _fake_settings())  # no accounts, registration closed
    html = auth.render_login_page()
    assert "Nothing's set up yet" in html
    assert "<form" not in html


def test_render_login_page_shows_form_when_accounts_exist(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html = auth.render_login_page()
    assert '<form method="post" action="/login">' in html


def test_render_login_page_shows_form_when_registration_open_even_with_no_accounts(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: True, has_smtp_credentials=lambda: True))
    html = auth.render_login_page()
    assert '<form method="post" action="/login">' in html
    assert 'href="/signup"' in html


def test_render_login_page_hides_signup_link_when_registration_closed(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html = auth.render_login_page()
    assert 'href="/signup"' not in html


def test_render_login_page_escapes_error_and_next(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html = auth.render_login_page(error="<script>alert(1)</script>", next_path="/x")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_login_page_falls_back_to_root_for_unsafe_next(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html = auth.render_login_page(next_path="//evil.com")
    assert 'value="/"' in html


def test_render_login_page_shows_resend_form(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": False}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html = auth.render_login_page(show_resend=True, resend_email="fenix@example.com")
    assert 'action="/resend-verification"' in html
    assert 'value="fenix@example.com"' in html


# --- Signup page rendering ---

def test_render_signup_page_disabled_notice_when_registration_closed(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: False, has_smtp_credentials=lambda: False))
    html = auth.render_signup_page()
    assert "isn't turned on" in html
    assert "<form" not in html


def test_render_signup_page_shows_form_when_open(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: True, has_smtp_credentials=lambda: True))
    html = auth.render_signup_page()
    assert '<form method="post" action="/signup">' in html


def test_render_signup_page_prefills_and_escapes_email(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_registration_code=lambda: True, has_smtp_credentials=lambda: True))
    html = auth.render_signup_page(email_value='"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in html


def test_render_verify_result_page_success_and_failure():
    success_html = auth.render_verify_result_page(success=True)
    assert "verified" in success_html.lower()
    failure_html = auth.render_verify_result_page(success=False)
    assert "invalid or has expired" in failure_html


# --- Admin: accounts page rendering ---

def _account(email, verified=True, group="", created_at="2026-01-01T00:00:00+00:00"):
    return SimpleNamespace(email=email, verified=verified, group=group, created_at=created_at)


def test_render_admin_users_page_lists_accounts():
    accounts = [_account("b@example.com"), _account("a@example.com")]
    html_out = auth.render_admin_users_page(accounts, current_email=None)
    assert "a@example.com" in html_out and "b@example.com" in html_out


def test_render_admin_users_page_default_sort_is_email_ascending():
    accounts = [_account("zed@example.com"), _account("amy@example.com")]
    html_out = auth.render_admin_users_page(accounts, current_email=None)
    assert html_out.index("amy@example.com") < html_out.index("zed@example.com")


def test_render_admin_users_page_sorts_by_group():
    accounts = [
        _account("a@example.com", group="zeta"),
        _account("b@example.com", group="alpha"),
        _account("c@example.com", group=""),
    ]
    html_out = auth.render_admin_users_page(accounts, current_email=None, sort="group", sort_dir="asc")
    # Blank group ("") sorts before any named group in ascending order.
    assert html_out.index("c@example.com") < html_out.index("b@example.com") < html_out.index("a@example.com")


def test_render_admin_users_page_sort_desc_reverses_order():
    accounts = [_account("amy@example.com"), _account("zed@example.com")]
    html_out = auth.render_admin_users_page(accounts, current_email=None, sort="email", sort_dir="desc")
    assert html_out.index("zed@example.com") < html_out.index("amy@example.com")


def test_render_admin_users_page_invalid_sort_falls_back_to_email():
    accounts = [_account("zed@example.com"), _account("amy@example.com")]
    html_out = auth.render_admin_users_page(accounts, current_email=None, sort="not-a-real-field")
    assert html_out.index("amy@example.com") < html_out.index("zed@example.com")


def test_render_admin_users_page_marks_current_user_and_hides_delete():
    accounts = [_account("me@example.com")]
    html_out = auth.render_admin_users_page(accounts, current_email="me@example.com")
    assert "(you)" in html_out
    assert "/admin/users/delete?email=" not in html_out


def test_render_admin_users_page_has_export_csv_link():
    html_out = auth.render_admin_users_page([_account("a@example.com")], current_email=None)
    assert 'href="/admin/users/export.csv"' in html_out


def test_render_admin_delete_confirm_page_carries_sort_state():
    html_out = auth.render_admin_delete_confirm_page("someone@example.com", sort="group", sort_dir="desc")
    assert 'name="sort" value="group"' in html_out
    assert 'name="dir" value="desc"' in html_out
    assert "someone@example.com" in html_out


# --- Forgot / reset password page rendering ---

# --- Welcome intro + guest registration code hint ---

def test_render_login_page_shows_welcome_intro_when_not_configured(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html_out = auth.render_login_page()
    assert "Welcome to" in html_out
    assert "Stock Analysis" in html_out


def test_render_login_page_shows_welcome_intro_when_configured(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html_out = auth.render_login_page()
    assert "Welcome to" in html_out


def test_render_signup_page_shows_guest_code_hint_when_configured(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _fake_settings(
            has_registration_code=lambda: True,
            has_smtp_credentials=lambda: True,
            registration_codes=lambda: {"GUEST": "0000", "FAMILY": "secret"},
        ),
    )
    html_out = auth.render_signup_page()
    assert "if you don't have one" in html_out
    assert "0000" in html_out
    assert "secret" not in html_out  # only the guest code is ever surfaced


def test_render_signup_page_omits_guest_hint_when_no_guest_code(monkeypatch):
    monkeypatch.setattr(
        auth,
        "settings",
        _fake_settings(
            has_registration_code=lambda: True,
            has_smtp_credentials=lambda: True,
            registration_codes=lambda: {"FAMILY": "secret"},
        ),
    )
    html_out = auth.render_signup_page()
    assert "if you don't have one" not in html_out


def test_render_login_page_shows_forgot_password_link(monkeypatch):
    monkeypatch.setattr(auth, "user_store", _fake_user_store({"fenix@example.com": {"password": "pw", "verified": True}}))
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html_out = auth.render_login_page()
    assert 'href="/forgot-password"' in html_out


def test_render_forgot_password_page_disabled_notice_without_smtp(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_smtp_credentials=lambda: False))
    html_out = auth.render_forgot_password_page()
    assert "isn't available" in html_out
    assert "<form" not in html_out


def test_render_forgot_password_page_shows_form_with_smtp(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings(has_smtp_credentials=lambda: True))
    html_out = auth.render_forgot_password_page()
    assert '<form method="post" action="/forgot-password">' in html_out


def test_render_forgot_password_sent_page_is_generic():
    html_out = auth.render_forgot_password_sent_page()
    assert "reset the" in html_out.lower()
    assert "@" not in html_out  # never echoes back an email either way


def test_render_reset_password_page_embeds_token_and_escapes_it(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html_out = auth.render_reset_password_page('abc"><script>alert(1)</script>')
    assert "<script>alert(1)</script>" not in html_out
    assert 'name="token"' in html_out


def test_render_reset_password_page_shows_error(monkeypatch):
    monkeypatch.setattr(auth, "settings", _fake_settings())
    html_out = auth.render_reset_password_page("sometoken", error="Passwords didn't match.")
    assert "Passwords didn&#x27;t match." in html_out or "Passwords didn't match." in html_out


def test_render_reset_password_invalid_page():
    html_out = auth.render_reset_password_invalid_page()
    assert "invalid or has expired" in html_out
    assert 'href="/forgot-password"' in html_out


def test_render_reset_password_success_page():
    html_out = auth.render_reset_password_success_page()
    assert "reset" in html_out.lower()
    assert 'href="/login"' in html_out
