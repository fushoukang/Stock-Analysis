"""Login accounts for the web GUI (see web/auth.py), keyed by email address.

Two ways an account gets created:
  - Self-service via the public /signup page (web/app.py) — gated by
    REGISTRATION_CODE and a required "click this link" email verification
    step, so the account starts unverified and can't log in yet.
  - Directly by you via `python manage_users.py add <email>` — created
    already verified, since there's no need to prove you own an email
    address you're typing into your own machine's terminal.

Persisted as a single JSON file (see settings.users_path, default
users.json), storing only a per-account salt and a PBKDF2-HMAC-SHA256 hash
of each password — never the password itself. Same low-volume, single-file
storage pattern as data/watchlists.py; a handful of personal accounts
doesn't need a real database.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from config import PROJECT_ROOT, settings

logger = logging.getLogger("data.users")

# OWASP's current recommended minimum for PBKDF2-HMAC-SHA256.
_PBKDF2_ITERATIONS = 600_000

# Deliberately simple — not a full RFC 5322 parse, just enough to catch
# obvious typos on the signup form. Real confirmation comes from the
# verification email link actually being clicked.
# Also excludes "|" (on top of "@"/whitespace) so an email can safely
# sit inside a "|"-delimited signed token (see web/auth.py) without
# any ambiguity about where the field boundaries are.
_EMAIL_RE = re.compile(r"^[^@\s|]+@[^@\s|]+\.[^@\s|]+$")


@dataclass
class User:
    email: str
    salt_hex: str
    hash_hex: str
    verified: bool = False
    created_at: str = ""
    # Optional label tagging which registration code (see
    # config.Settings.registration_codes) this account signed up with —
    # e.g. "FAMILY", "FRIENDS", "GENERAL". Stored upper-cased. Purely for
    # your own record-keeping (see `python manage_users.py list`); it has
    # no effect on what the account can do. Blank for accounts created
    # directly via manage_users.py without --group.
    group: str = ""


def _path() -> Path:
    p = Path(settings.users_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS).hex()


def normalize_email(email: str) -> str:
    return email.strip().lower()


def looks_like_email(email: str) -> bool:
    return bool(_EMAIL_RE.match(email.strip()))


def load_users() -> list[User]:
    p = _path()
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read/parse %s — treating as empty.", p, exc_info=True)
        return []
    items = raw.get("users", []) if isinstance(raw, dict) else []
    result = []
    for item in items:
        try:
            result.append(
                User(
                    email=str(item["email"]),
                    salt_hex=str(item["salt_hex"]),
                    hash_hex=str(item["hash_hex"]),
                    verified=bool(item.get("verified", False)),
                    created_at=str(item.get("created_at", "")),
                    group=str(item.get("group", "")),
                )
            )
        except (KeyError, TypeError):
            logger.warning("Skipping malformed entry in %s", p)
    return result


def save_users(users: list[User]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"users": [asdict(u) for u in users]}
    # Write to a temp file then rename, so a crash mid-write can't leave
    # users.json truncated/corrupted.
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    tmp.replace(p)


def list_users() -> list[User]:
    return load_users()


def user_exists(email: str) -> bool:
    norm = normalize_email(email)
    return any(u.email == norm for u in load_users())


def is_verified(email: str) -> bool:
    norm = normalize_email(email)
    return any(u.email == norm and u.verified for u in load_users())


def is_active(email: str) -> bool:
    """A session/login is only ever valid for an account that both still
    exists and has completed email verification."""
    return is_verified(email)


def add_user(email: str, password: str, *, verified: bool = False, group: str = "") -> User:
    norm = normalize_email(email)
    if not looks_like_email(norm):
        raise ValueError(f"{email!r} doesn't look like a valid email address.")
    if not password:
        raise ValueError("Password can't be blank.")
    users = load_users()
    if any(u.email == norm for u in users):
        raise ValueError(f"An account for {norm!r} already exists.")
    salt = secrets.token_bytes(16)
    user = User(
        email=norm,
        salt_hex=salt.hex(),
        hash_hex=_hash_password(password, salt),
        verified=verified,
        created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        group=group.strip().upper(),
    )
    users.append(user)
    save_users(users)
    return user


def mark_verified(email: str) -> bool:
    norm = normalize_email(email)
    users = load_users()
    for i, u in enumerate(users):
        if u.email == norm:
            if u.verified:
                return True
            users[i] = User(
                email=u.email,
                salt_hex=u.salt_hex,
                hash_hex=u.hash_hex,
                verified=True,
                created_at=u.created_at,
                group=u.group,
            )
            save_users(users)
            return True
    return False


def remove_user(email: str) -> bool:
    norm = normalize_email(email)
    users = load_users()
    remaining = [u for u in users if u.email != norm]
    if len(remaining) == len(users):
        return False
    save_users(remaining)
    return True


def set_password(email: str, password: str) -> bool:
    norm = normalize_email(email)
    if not password:
        raise ValueError("Password can't be blank.")
    users = load_users()
    for i, u in enumerate(users):
        if u.email == norm:
            salt = secrets.token_bytes(16)
            users[i] = User(
                email=norm,
                salt_hex=salt.hex(),
                hash_hex=_hash_password(password, salt),
                verified=u.verified,
                created_at=u.created_at,
                group=u.group,
            )
            save_users(users)
            return True
    return False


def verify_password(email: str, password: str) -> bool:
    norm = normalize_email(email)
    for u in load_users():
        if u.email == norm:
            expected = _hash_password(password, bytes.fromhex(u.salt_hex))
            return hmac.compare_digest(expected, u.hash_hex)
    # No such account — still do a dummy hash so a nonexistent email doesn't
    # respond measurably faster than a wrong password would.
    _hash_password(password, secrets.token_bytes(16))
    return False
