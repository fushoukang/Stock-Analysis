"""
CLI for managing the web GUI's login accounts (see data/users.py,
web/auth.py). Most people should just use the public /signup page (email +
password + your REGISTRATION_CODE) — this script is for you, the person
running the app: create your own account without the email round trip,
remove someone's access, reset a forgotten password, or see who's signed
up.

Accounts created here are marked verified immediately (no confirmation
email needed — you're already trusting yourself by typing into your own
machine's terminal).

Usage:
    python manage_users.py add <email> [--group NAME]  # prompts for a password, pre-verified
    python manage_users.py passwd <email>               # prompts for a new password
    python manage_users.py remove <email>
    python manage_users.py list

--group is just a bookkeeping label (shown in `list`), the same idea as
the group a REGISTRATION_CODE_<GROUP> self-service signup gets tagged
with (see config.py) — it has no effect on what the account can do.
"""
from __future__ import annotations

import argparse
import getpass
import sys

from data import users as user_store


def _prompt_password(label: str = "Password") -> str:
    while True:
        pw1 = getpass.getpass(f"{label}: ")
        if not pw1:
            print("Password can't be blank.", file=sys.stderr)
            continue
        pw2 = getpass.getpass(f"{label} (again): ")
        if pw1 != pw2:
            print("Passwords didn't match — try again.", file=sys.stderr)
            continue
        return pw1


def cmd_add(args: argparse.Namespace) -> int:
    password = _prompt_password()
    try:
        user = user_store.add_user(args.email, password, verified=True, group=args.group)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    group_note = f" (group: {user.group})" if user.group else ""
    print(f"Added and verified {user.email!r}{group_note}.")
    return 0


def cmd_passwd(args: argparse.Namespace) -> int:
    if not user_store.user_exists(args.email):
        print(f"Error: no account for {args.email!r}. Run `list` to see current accounts.", file=sys.stderr)
        return 1
    password = _prompt_password("New password")
    user_store.set_password(args.email, password)
    print(f"Password updated for {args.email!r}. They'll need to sign in again.")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    if not user_store.remove_user(args.email):
        print(f"Error: no account for {args.email!r}.", file=sys.stderr)
        return 1
    print(f"Removed {args.email!r}. Their active sessions stop working immediately.")
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    accounts = user_store.list_users()
    if not accounts:
        print("No accounts yet — add one with: python manage_users.py add <email>")
        return 0
    for u in accounts:
        status = "verified" if u.verified else "unverified (never clicked their email link)"
        group = f", group: {u.group}" if u.group else ""
        created = f", created {u.created_at}" if u.created_at else ""
        print(f"{u.email} — {status}{group}{created}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage web GUI login accounts.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add", help="Create a new, pre-verified account (prompts for a password).")
    p_add.add_argument("email")
    p_add.add_argument(
        "--group",
        default="",
        help=(
            "Optional label for this account (e.g. \"family\"), same idea as the "
            "group a REGISTRATION_CODE_<GROUP> signup gets tagged with — shown in "
            "`list` for your own bookkeeping, doesn't affect access."
        ),
    )
    p_add.set_defaults(func=cmd_add)

    p_passwd = sub.add_parser("passwd", help="Change an existing account's password.")
    p_passwd.add_argument("email")
    p_passwd.set_defaults(func=cmd_passwd)

    p_remove = sub.add_parser("remove", help="Delete an account.")
    p_remove.add_argument("email")
    p_remove.set_defaults(func=cmd_remove)

    p_list = sub.add_parser("list", help="List current accounts and their verification status.")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
