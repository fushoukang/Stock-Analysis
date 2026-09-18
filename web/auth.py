"""
Authentication for the web GUI.

Named accounts (email + password) gate every page, every /api/* route, and
the /ws WebSocket — see web/app.py's `require_login` middleware and the
/login, /signup, /verify, /logout, /resend-verification routes for how this
module gets used. Two ways to get an account: self-service via /signup
(gated by REGISTRATION_CODE + a required email-verification link) or
directly via `python manage_users.py add <email>` (pre-verified).

Sessions and email-verification links are both signed tokens, not
server-side state: each cookie/link holds a purpose tag ("session" or
"verify", so one can never be replayed as the other), the account's email,
an expiry timestamp, and an HMAC-SHA256 signature over all three — so the
server can trust "this was issued by us, for this purpose, for this
account, and hasn't expired or been tampered with" without keeping any
per-token state in memory or a database. Deliberately stdlib-only
(hmac/hashlib/secrets) — no new dependency (e.g. itsdangerous) to install.
On every request the signed email is also re-checked against
data/users.py, so removing someone's account (or an account that's never
been verified) logs them out / locks them out immediately, not just once
their token happens to expire.

Failing closed: if no accounts exist yet and self-service signup isn't
configured (REGISTRATION_CODE unset), the GUI still requires a login
(nothing is reachable without one) but the login form can never succeed,
and says so — so forgetting to set things up can't silently leave the
dashboard open on the network. Signup itself is separately gated on SMTP
being configured (see config.has_smtp_credentials) — without it there's no
way to deliver the verification link, so /signup says so rather than
creating accounts nobody can ever activate.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import logging
import secrets
import time
from urllib.parse import quote

from config import settings
from data import users as user_store

logger = logging.getLogger("web.auth")

SESSION_COOKIE_NAME = "stx_session"

_PURPOSE_SESSION = "session"
_PURPOSE_VERIFY = "verify"
_PURPOSE_RESET = "reset"

# --- Token signing ---
# Lazily generated so a missing SESSION_SECRET_KEY doesn't crash the app —
# it just means sessions (and any verification link already sent) reset on
# every restart (logged once, below).
_generated_secret: bytes | None = None
_warned_no_secret = False


def _secret_key() -> bytes:
    global _generated_secret, _warned_no_secret
    if settings.session_secret_key:
        # sha256 normalizes any provided string to a fixed-length key.
        return hashlib.sha256(settings.session_secret_key.encode("utf-8")).digest()
    if _generated_secret is None:
        _generated_secret = secrets.token_bytes(32)
        if not _warned_no_secret:
            logger.warning(
                "SESSION_SECRET_KEY not set in .env — using a random key "
                "generated for this run, so everyone will be logged out (and "
                "any verification email already sent will stop working) on "
                "the next restart. Set SESSION_SECRET_KEY to keep these "
                "persistent across restarts."
            )
            _warned_no_secret = True
    return _generated_secret


def _create_token(purpose: str, email: str, max_age_hours: int) -> str:
    """A cookie/link value of "<purpose>|<email>|<expires_at_unix>.<hmac_hex>".
    Plain text, not encrypted — it carries no secret data, just what the
    server can trust because of the signature. Emails are restricted (see
    data/users.py's looks_like_email) to never contain "|" or whitespace, so
    splitting the payload back apart on "|" is unambiguous."""
    expires_at = int(time.time()) + max_age_hours * 3600
    payload = f"{purpose}|{email}|{expires_at}"
    signature = hmac.new(_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _verify_token(token: str | None, expected_purpose: str) -> str | None:
    """Returns the token's email if it's validly signed, unexpired, and
    tagged with `expected_purpose` — else None."""
    if not token or "." not in token:
        return None
    payload, _, signature = token.rpartition(".")
    parts = payload.split("|")
    if len(parts) != 3:
        return None
    purpose, email, expires_at = parts
    if purpose != expected_purpose or not email or not expires_at.isdigit():
        return None
    expected_signature = hmac.new(_secret_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_signature, signature):
        return None
    if int(expires_at) <= time.time():
        return None
    return email


def create_session_token(email: str) -> str:
    return _create_token(_PURPOSE_SESSION, email, settings.session_max_age_hours)


def verify_session_token(token: str | None) -> str | None:
    email = _verify_token(token, _PURPOSE_SESSION)
    if email is None:
        return None
    # Re-checked on every request: an account removed (or somehow
    # unverified) after the cookie was issued stops working immediately,
    # rather than waiting out the token's expiry.
    if not user_store.is_active(email):
        return None
    return email


def create_verification_token(email: str) -> str:
    return _create_token(_PURPOSE_VERIFY, email, settings.verification_token_max_age_hours)


def verify_verification_token(token: str | None) -> str | None:
    email = _verify_token(token, _PURPOSE_VERIFY)
    if email is None or not user_store.user_exists(email):
        return None
    return email


def create_reset_token(email: str) -> str:
    return _create_token(_PURPOSE_RESET, email, settings.reset_token_max_age_hours)


def verify_reset_token(token: str | None) -> str | None:
    """Same shape as verify_verification_token — re-checks the account
    still exists (not just that the signature/expiry are valid), so a
    reset link for a since-removed account stops working immediately."""
    email = _verify_token(token, _PURPOSE_RESET)
    if email is None or not user_store.user_exists(email):
        return None
    return email


def get_current_email(conn) -> str | None:
    """`conn` is anything with a `.cookies` mapping — a FastAPI/Starlette
    Request or WebSocket both qualify, so this works for both the HTTP
    middleware and the /ws handshake check."""
    return verify_session_token(conn.cookies.get(SESSION_COOKIE_NAME))


def is_authenticated(conn) -> bool:
    return get_current_email(conn) is not None


def is_admin(conn) -> bool:
    """Whether the currently signed-in account (if any) is listed in
    ADMIN_EMAILS — the gate for /admin/users (see web/app.py's
    require_login middleware, which checks this separately from plain
    is_authenticated)."""
    email = get_current_email(conn)
    return email is not None and settings.is_admin(email)


# --- Credential check ---
def has_any_users() -> bool:
    return bool(user_store.load_users())


def registration_open() -> bool:
    return settings.has_registration_code() and settings.has_smtp_credentials()


def group_for_code(code: str) -> str | None:
    """Which group (if any) a submitted /signup registration code belongs
    to — None if it doesn't match any configured code (see
    config.Settings.registration_codes). Checks every configured code
    rather than stopping at the first match, so a wrong guess takes the
    same time regardless of how many groups are configured."""
    matched: str | None = None
    for group, candidate in settings.registration_codes().items():
        if hmac.compare_digest(code, candidate):
            matched = group
    return matched


def check_credentials(email: str, password: str) -> str | None:
    """Password match only — returns the canonical (normalized) email on
    success, else None. Deliberately doesn't gate on verified/existing here,
    so callers (see web/app.py's /login) can tell "wrong password" apart
    from "right password, unverified account" and say so specifically."""
    if not has_any_users():
        return None
    norm = user_store.normalize_email(email)
    if not user_store.verify_password(norm, password):
        return None
    return norm


# --- Per-IP rate limiting (login attempts, signups, resend requests — the
# caller namespaces the key, e.g. f"login:{ip}" vs f"signup:{ip}", so a
# lockout on one action doesn't also block the others from the same IP) ---
# Plain in-memory dict — this app has one process and no load balancer, so
# there's nowhere else state like this would need to live.
_MAX_ATTEMPTS = 5
_WINDOW_SEC = 60.0
_failed_attempts: dict[str, list[float]] = {}


def is_rate_limited(key: str) -> bool:
    now = time.time()
    attempts = [t for t in _failed_attempts.get(key, []) if now - t < _WINDOW_SEC]
    _failed_attempts[key] = attempts
    return len(attempts) >= _MAX_ATTEMPTS


def record_failed_attempt(key: str) -> None:
    _failed_attempts.setdefault(key, []).append(time.time())


def reset_attempts(key: str) -> None:
    _failed_attempts.pop(key, None)


# --- Open-redirect guard for ?next=... ---
def is_safe_redirect_path(path: str) -> bool:
    """Only ever redirect to a same-site, absolute path — never to a
    scheme-relative ("//evil.com") or fully-qualified URL."""
    if not path or not path.startswith("/"):
        return False
    if path.startswith("//"):
        return False
    if "://" in path:
        return False
    return True


# --- Verification email content ---
def verification_email_content(verify_url: str) -> tuple[str, str]:
    subject = "Confirm your Stock Analysis account"
    body = (
        "Someone (hopefully you) signed up for Stock Analysis with "
        "this email address.\n\n"
        "Click this link to verify your email and activate your account:\n"
        f"{verify_url}\n\n"
        f"This link expires in {settings.verification_token_max_age_hours} hours. "
        "If you didn't request this, you can ignore this email."
    )
    return subject, body


# --- Password reset email content ---
def reset_password_email_content(reset_url: str) -> tuple[str, str]:
    hours = settings.reset_token_max_age_hours
    subject = "Reset your Stock Analysis password"
    body = (
        "Someone (hopefully you) asked to reset the password for this "
        "Stock Analysis account.\n\n"
        "Click this link to set a new password:\n"
        f"{reset_url}\n\n"
        f"This link expires in {hours} hour{'s' if hours != 1 else ''}. "
        "If you didn't request this, you can ignore this email — your "
        "password won't change unless that link is used."
    )
    return subject, body


# --- Shared page chrome (kept intentionally tiny/inline — no static-asset
# dependency, so /login and /signup work even though everything under
# /static requires a session, per web/app.py's require_login middleware) ---
_PAGE_STYLE = """
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f7f8fa; color: #1a1a1a;
         display: flex; align-items: center; justify-content: center; min-height: 100vh; }
  .card { background: white; border-radius: 8px; box-shadow: 0 4px 20px rgba(0,0,0,0.08); padding: 32px; width: 340px; }
  h1 { font-size: 16px; margin: 0 0 4px; color: #111827; }
  .subtitle { font-size: 12px; color: #6b7280; margin: 0 0 20px; }
  label { display: block; font-size: 13px; color: #374151; margin-bottom: 12px; }
  input { display: block; width: 100%; box-sizing: border-box; margin-top: 4px; padding: 8px 10px;
          font-size: 14px; border: 1px solid #d1d5db; border-radius: 4px; }
  button { width: 100%; padding: 9px; font-size: 14px; font-weight: 600; color: white; background: #2563eb;
           border: none; border-radius: 4px; cursor: pointer; margin-top: 4px; }
  button:hover { background: #1d4ed8; }
  button.secondary { background: white; color: #2563eb; border: 1px solid #2563eb; margin-top: 10px; }
  button.secondary:hover { background: #eff6ff; }
  .error { background: #fee2e2; color: #b91c1c; font-size: 13px; padding: 8px 10px; border-radius: 4px; margin-bottom: 14px; }
  .info { background: #dcfce7; color: #15803d; font-size: 13px; padding: 8px 10px; border-radius: 4px; margin-bottom: 14px; }
  .notice { background: #fef3c7; color: #92400e; font-size: 13px; padding: 10px 12px; border-radius: 4px; line-height: 1.5; }
  .notice code { background: rgba(0,0,0,0.06); padding: 1px 4px; border-radius: 3px; }
  .welcome { font-size: 13px; color: #4b5563; line-height: 1.5; margin-bottom: 18px; padding-bottom: 16px; border-bottom: 1px solid #e5e7eb; }
  .welcome p { margin: 0 0 8px; }
  .welcome p:last-child { margin-bottom: 0; }
  .footer-link { margin-top: 16px; font-size: 12px; color: #6b7280; text-align: center; }
  .footer-link a { color: #2563eb; text-decoration: none; }
  .footer-link a:hover { text-decoration: underline; }
  .card.wide { width: 640px; max-width: 90vw; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 8px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e5e7eb; vertical-align: middle; }
  th { color: #6b7280; font-weight: 600; font-size: 11px; text-transform: uppercase; }
  .badge { display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 11px; font-weight: 600; }
  .badge.verified { background: #dcfce7; color: #15803d; }
  .badge.unverified { background: #fef3c7; color: #92400e; }
  .you-note { color: #9ca3af; font-size: 11px; }
  .danger-btn { background: #dc2626; color: white; border: none; border-radius: 4px; padding: 5px 10px; font-size: 12px; cursor: pointer; }
  .danger-btn:hover { background: #b91c1c; }
"""


def _page(title: str, body: str, *, wide: bool = False) -> str:
    card_class = "card wide" if wide else "card"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)} — Real Time Stock Trend Analysis</title>
<style>{_PAGE_STYLE}</style>
</head>
<body>
  <div class="{card_class}">
    <h1>Real Time Stock Trend Analysis</h1>
    <p class="subtitle">{html.escape(title)}</p>
    {body}
  </div>
</body>
</html>"""


# --- Login page ---
# Shown to any signed-out visitor — the first thing anyone hitting this app
# ever sees, whether they already have an account or are arriving cold with
# no idea what this software even does. WELCOME_HTML is deliberately a
# short, generic pitch (not tied to whether login/signup is configured yet)
# so it appears in both branches below.
WELCOME_HTML = """
<div class="welcome">
  <p>Welcome to <strong>Real Time Stock Trend Analysis</strong> — a real-time dashboard for
  stock and crypto markets, built on Alpaca's market data.</p>
  <p>Track live candlestick charts with technical indicators (RSI, MACD,
  KDJ, Bollinger Bands, SuperTrend, and more), per-indicator trend signals,
  KDJ cross alerts, and personal watchlists — all in one place.</p>
</div>
"""


def render_login_page(
    *,
    error: str | None = None,
    info: str | None = None,
    next_path: str = "/",
    show_resend: bool = False,
    resend_email: str = "",
) -> str:
    safe_next = html.escape(next_path if is_safe_redirect_path(next_path) else "/", quote=True)
    configured = has_any_users() or registration_open()

    if not configured:
        body = f"""
        {WELCOME_HTML}
        <div class="notice">
          Nothing's set up yet. Either create your own account with
          <code>python manage_users.py add &lt;email&gt;</code>, or set
          <code>REGISTRATION_CODE</code> (and SMTP credentials) in
          <code>.env</code> to turn on self-service signup, then restart
          the app.
        </div>
        """
        return _page("Sign in", body)

    info_html = f'<div class="info">{html.escape(info)}</div>' if info else ""
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    resend_html = ""
    if show_resend:
        safe_resend_email = html.escape(resend_email, quote=True)
        resend_html = f"""
        <form method="post" action="/resend-verification">
          <input type="hidden" name="email" value="{safe_resend_email}">
          <button type="submit" class="secondary">Resend verification email</button>
        </form>
        """
    signup_link = (
        '<div class="footer-link">No account? <a href="/signup">Sign up</a></div>'
        if registration_open()
        else ""
    )

    body = f"""
    {WELCOME_HTML}
    {info_html}
    {error_html}
    <form method="post" action="/login">
      <input type="hidden" name="next" value="{safe_next}">
      <label>Email
        <input type="email" name="email" autocomplete="username" autofocus required>
      </label>
      <label>Password
        <input type="password" name="password" autocomplete="current-password" required>
      </label>
      <button type="submit">Sign in</button>
    </form>
    <div class="footer-link"><a href="/forgot-password">Forgot password?</a></div>
    {resend_html}
    {signup_link}
    """
    return _page("Sign in", body)


# --- Signup page ---
def render_signup_page(*, error: str | None = None, email_value: str = "") -> str:
    if not registration_open():
        missing = []
        if not settings.has_registration_code():
            missing.append("<code>REGISTRATION_CODE</code> (or a <code>REGISTRATION_CODE_&lt;GROUP&gt;</code>)")
        if not settings.has_smtp_credentials():
            missing.append("SMTP credentials (<code>SMTP_HOST</code>/<code>SMTP_USERNAME</code>/<code>SMTP_PASSWORD</code>)")
        body = f"""
        <div class="notice">
          Self-service signup isn't turned on. Set {" and ".join(missing)}
          in <code>.env</code> and restart the app to enable it — or ask
          whoever runs this app to create your account with
          <code>python manage_users.py add &lt;email&gt;</code>.
        </div>
        <div class="footer-link"><a href="/login">Back to sign in</a></div>
        """
        return _page("Sign up", body)

    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    safe_email = html.escape(email_value, quote=True)
    # A "GUEST" registration code is meant to be public/low-friction (unlike
    # a real code you'd hand out privately), so surface it directly here
    # rather than making a new visitor guess what to type. Pulled live from
    # settings.registration_codes() rather than hardcoded, so this stays
    # correct if the code is ever changed or removed from .env.
    guest_code = settings.registration_codes().get("GUEST")
    guest_hint_html = (
        f'<div class="notice">Enter <code>{html.escape(guest_code)}</code> as the '
        "registration code below if you don't have one.</div>"
        if guest_code
        else ""
    )
    body = f"""
    {guest_hint_html}
    {error_html}
    <form method="post" action="/signup">
      <label>Email
        <input type="email" name="email" value="{safe_email}" autocomplete="username" autofocus required>
      </label>
      <label>Password
        <input type="password" name="password" autocomplete="new-password" required minlength="8">
      </label>
      <label>Confirm password
        <input type="password" name="confirm_password" autocomplete="new-password" required minlength="8">
      </label>
      <label>Registration code
        <input type="password" name="registration_code" autocomplete="off" required>
      </label>
      <button type="submit">Sign up</button>
    </form>
    <div class="footer-link">Already have an account? <a href="/login">Sign in</a></div>
    """
    return _page("Sign up", body)


def render_signup_sent_page(email: str) -> str:
    body = f"""
    <div class="info">
      We've sent a verification link to <strong>{html.escape(email)}</strong>.
      Click it to activate your account — it expires in
      {settings.verification_token_max_age_hours} hours.
    </div>
    <div class="footer-link"><a href="/login">Back to sign in</a></div>
    """
    return _page("Check your email", body)


# --- Forgot / reset password ---
def render_forgot_password_page(*, error: str | None = None) -> str:
    if not settings.has_smtp_credentials():
        body = """
        <div class="notice">
          Password reset isn't available — SMTP credentials
          (<code>SMTP_HOST</code>/<code>SMTP_USERNAME</code>/<code>SMTP_PASSWORD</code>)
          aren't configured. Ask whoever runs this app to reset your
          password with <code>python manage_users.py passwd &lt;email&gt;</code>.
        </div>
        <div class="footer-link"><a href="/login">Back to sign in</a></div>
        """
        return _page("Reset password", body)

    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = f"""
    {error_html}
    <form method="post" action="/forgot-password">
      <label>Email
        <input type="email" name="email" autocomplete="username" autofocus required>
      </label>
      <button type="submit">Send reset link</button>
    </form>
    <div class="footer-link"><a href="/login">Back to sign in</a></div>
    """
    return _page("Reset password", body)


def render_forgot_password_sent_page() -> str:
    # Deliberately doesn't say whether the email actually had an account —
    # same anti-enumeration reasoning as /resend-verification's generic
    # message.
    body = """
    <div class="info">
      If that email has an account here, we've sent a link to reset the
      password.
    </div>
    <div class="footer-link"><a href="/login">Back to sign in</a></div>
    """
    return _page("Check your email", body)


def render_reset_password_page(token: str, *, error: str | None = None) -> str:
    safe_token = html.escape(token, quote=True)
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    body = f"""
    {error_html}
    <form method="post" action="/reset-password">
      <input type="hidden" name="token" value="{safe_token}">
      <label>New password
        <input type="password" name="password" autocomplete="new-password" required minlength="8" autofocus>
      </label>
      <label>Confirm new password
        <input type="password" name="confirm_password" autocomplete="new-password" required minlength="8">
      </label>
      <button type="submit">Set new password</button>
    </form>
    <div class="footer-link"><a href="/login">Back to sign in</a></div>
    """
    return _page("Set a new password", body)


def render_reset_password_invalid_page() -> str:
    body = """
    <div class="error">This reset link is invalid or has expired.</div>
    <div class="footer-link"><a href="/forgot-password">Request a new link</a></div>
    """
    return _page("Reset password", body)


def render_reset_password_success_page() -> str:
    body = """
    <div class="info">Your password has been reset — you can sign in now.</div>
    <div class="footer-link"><a href="/login">Go to sign in</a></div>
    """
    return _page("Password reset", body)


def render_verify_result_page(*, success: bool) -> str:
    if success:
        body = """
        <div class="info">Your email is verified — you can sign in now.</div>
        <div class="footer-link"><a href="/login">Go to sign in</a></div>
        """
    else:
        body = """
        <div class="error">
          This verification link is invalid or has expired.
        </div>
        <div class="footer-link"><a href="/signup">Sign up again</a></div>
        """
    return _page("Email verification", body)


# --- 403 for a logged-in-but-not-admin visitor to /admin/* ---
def render_forbidden_page() -> str:
    body = """
    <div class="error">You don't have access to this page.</div>
    <div class="footer-link"><a href="/">Back to the dashboard</a></div>
    """
    return _page("Forbidden", body)


# --- Admin: view/delete accounts (see web/app.py's /admin/users routes,
# gated on auth.is_admin — ADMIN_EMAILS in .env) ---

# Column -> sort key. Each returns a tuple so ties break on email (stable,
# predictable ordering) rather than on whatever order data/users.py's JSON
# file happens to list accounts in.
_ADMIN_SORT_KEYS = {
    "email": lambda u: (u.email,),
    "status": lambda u: (not u.verified, u.email),
    # Blank groups sort first in ascending order (empty string < any other
    # string) — flip to "sort=group&dir=desc" to push them to the bottom.
    "group": lambda u: (u.group, u.email),
    "created": lambda u: (u.created_at, u.email),
}
_ADMIN_SORT_COLUMNS = [
    ("email", "Email"),
    ("status", "Status"),
    ("group", "Group"),
    ("created", "Created"),
]


def _admin_sort_header(field: str, label: str, current_sort: str, current_dir: str) -> str:
    next_dir = "desc" if current_sort == field and current_dir == "asc" else "asc"
    arrow = ""
    if current_sort == field:
        arrow = " ▲" if current_dir == "asc" else " ▼"
    return (
        f'<a href="/admin/users?sort={field}&dir={next_dir}" '
        f'style="color:inherit;text-decoration:none;">{label}{arrow}</a>'
    )


def render_admin_users_page(
    accounts: list,
    *,
    current_email: str | None,
    error: str | None = None,
    deleted: str = "",
    sort: str = "email",
    sort_dir: str = "asc",
) -> str:
    error_html = f'<div class="error">{html.escape(error)}</div>' if error else ""
    deleted_html = (
        f'<div class="info">Deleted {html.escape(deleted)}.</div>' if deleted else ""
    )

    sort = sort if sort in _ADMIN_SORT_KEYS else "email"
    sort_dir = sort_dir if sort_dir in ("asc", "desc") else "asc"
    sort_qs = f"sort={sort}&dir={sort_dir}"

    if not accounts:
        rows_html = '<tr><td colspan="5" style="color:#9ca3af;">No accounts yet.</td></tr>'
    else:
        ordered = sorted(accounts, key=_ADMIN_SORT_KEYS[sort], reverse=(sort_dir == "desc"))
        rows = []
        for u in ordered:
            status_class = "verified" if u.verified else "unverified"
            status_label = "verified" if u.verified else "unverified"
            group = html.escape(u.group) if getattr(u, "group", "") else "—"
            created = html.escape(u.created_at) if u.created_at else "—"
            if u.email == current_email:
                action = '<span class="you-note">(you)</span>'
            else:
                action = (
                    f'<a href="/admin/users/delete?email={quote(u.email)}&{sort_qs}">'
                    f'<button type="button" class="danger-btn">Delete</button></a>'
                )
            rows.append(
                f"<tr><td>{html.escape(u.email)}</td>"
                f'<td><span class="badge {status_class}">{status_label}</span></td>'
                f"<td>{group}</td><td>{created}</td><td>{action}</td></tr>"
            )
        rows_html = "\n".join(rows)

    headers_html = "".join(
        f"<th>{_admin_sort_header(field, label, sort, sort_dir)}</th>"
        for field, label in _ADMIN_SORT_COLUMNS
    )

    body = f"""
    {error_html}
    {deleted_html}
    <table>
      <thead><tr>{headers_html}<th></th></tr></thead>
      <tbody>
        {rows_html}
      </tbody>
    </table>
    <div class="footer-link">
      <a href="/admin/users/export.csv">Export all as CSV</a> ·
      <a href="/">Back to the dashboard</a>
    </div>
    """
    return _page("Manage accounts", body, wide=True)


def render_admin_delete_confirm_page(email: str, *, sort: str = "email", sort_dir: str = "asc") -> str:
    safe_email = html.escape(email, quote=True)
    safe_sort = html.escape(sort, quote=True)
    safe_dir = html.escape(sort_dir, quote=True)
    body = f"""
    <div class="notice">
      Delete the account for <strong>{html.escape(email)}</strong>? This
      can't be undone — they'll need to sign up again (and re-verify) to
      get back in.
    </div>
    <form method="post" action="/admin/users/delete">
      <input type="hidden" name="email" value="{safe_email}">
      <input type="hidden" name="sort" value="{safe_sort}">
      <input type="hidden" name="dir" value="{safe_dir}">
      <button type="submit" class="danger-btn" style="width:100%;padding:9px;font-size:14px;">Delete account</button>
    </form>
    <div class="footer-link"><a href="/admin/users">Cancel</a></div>
    """
    return _page("Confirm delete", body)
