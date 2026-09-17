"""Not a pytest test file (no test_ prefix) — this is the script
tests/test_signup_flow.py runs in an ISOLATED SUBPROCESS, with its own
environment pointing DB_PATH/USERS_PATH/WATCHLISTS_PATH at throwaway files.

Why a subprocess at all: web/app.py builds its BarStore (and other
module-level state) from config.settings at *import* time, and by the time
any test in the shared pytest process gets to run, some other test file has
usually already imported config (or a module that imports it) using the
REAL .env — so config.settings would already be a fixed singleton pointed
at the real, 23MB data_store.db, and there'd be no safe way to redirect it
from inside that same process. A dedicated subprocess with the right
environment set *before* web.app is ever imported sidesteps that entirely:
this script only ever touches the tmp_path files the test handed it.

Prints "OK" and exits 0 on success; prints the failure and exits 1
otherwise, so tests/test_signup_flow.py can just check the return code.
"""
from __future__ import annotations

import sys
from urllib.parse import unquote


def main() -> int:
    sent_emails = []

    from fastapi.testclient import TestClient
    from data import users as user_store
    import web.app as app_module

    app_module.send_email_alert = lambda subject, body, to_address=None: (
        sent_emails.append((subject, body, to_address)) or True
    )

    client = TestClient(app_module.app)

    def check(condition, message):
        if not condition:
            print(f"FAIL: {message}", file=sys.stderr)
            sys.exit(1)

    # 1. Signup with the right registration code sends exactly one email
    # and does NOT log the user in yet (account is unverified).
    r = client.post(
        "/signup",
        data={
            "email": "New.User@Example.com",
            "password": "correct-horse-battery",
            "confirm_password": "correct-horse-battery",
            "registration_code": "let-me-in",
        },
    )
    check(r.status_code == 200 and "verification link" in r.text.lower(), f"signup response: {r.status_code} {r.text[:200]}")
    check(len(sent_emails) == 1, f"expected exactly 1 email, got {len(sent_emails)}")
    subject, body, to_address = sent_emails[0]
    check(to_address == "new.user@example.com", f"email normalization: {to_address!r}")
    stored = next((u for u in user_store.list_users() if u.email == "new.user@example.com"), None)
    check(stored is not None and stored.group == "GENERAL", f"signup via REGISTRATION_CODE should tag group 'GENERAL', got {stored.group if stored else None!r}")

    # 2. Logging in before verifying is rejected with a specific message
    # and a resend option, not a generic "incorrect" error.
    r = client.post("/login", data={"email": "new.user@example.com", "password": "correct-horse-battery"})
    check(r.status_code == 403 and "verify your email" in r.text.lower(), f"pre-verify login: {r.status_code}")
    check('action="/resend-verification"' in r.text, "resend form missing from pre-verify login error")

    # 3. Clicking the verification link activates the account.
    verify_url = next(line.strip() for line in body.splitlines() if line.strip().startswith("http"))
    token = verify_url.split("token=", 1)[1]
    r = client.get(f"/verify?token={token}")
    check(r.status_code == 200 and "verified" in r.text.lower(), f"verify: {r.status_code} {r.text[:200]}")

    # 4. Now login succeeds and sets a session cookie.
    r = client.post(
        "/login", data={"email": "new.user@example.com", "password": "correct-horse-battery"}, follow_redirects=False
    )
    check(r.status_code == 303 and "stx_session" in client.cookies, f"post-verify login: {r.status_code}")

    # 5. Authenticated access works and reports the signed-in email.
    r = client.get("/api/status")
    check(r.status_code == 200 and r.json().get("current_user") == "new.user@example.com", f"status: {r.status_code} {r.text[:200]}")

    # 6. Wrong registration code is rejected and sends no email.
    r = client.post(
        "/signup",
        data={
            "email": "someone@example.com",
            "password": "correct-horse-battery",
            "confirm_password": "correct-horse-battery",
            "registration_code": "wrong-code",
        },
    )
    check(r.status_code == 400 and "registration code" in r.text.lower(), f"bad reg code: {r.status_code}")
    check(len(sent_emails) == 1, "no email should have been sent for the rejected signup")

    # 7. Mismatched / short passwords are rejected.
    r = client.post(
        "/signup",
        data={
            "email": "someone@example.com",
            "password": "correct-horse-battery",
            "confirm_password": "different",
            "registration_code": "let-me-in",
        },
    )
    check(r.status_code == 400 and "match" in r.text.lower(), f"mismatched passwords: {r.status_code}")

    r = client.post(
        "/signup",
        data={
            "email": "someone@example.com",
            "password": "short1",
            "confirm_password": "short1",
            "registration_code": "let-me-in",
        },
    )
    check(r.status_code == 400 and "8 characters" in r.text, f"short password: {r.status_code}")

    # 8. A bad/garbage verification token shows the failure page, not a 500.
    r = client.get("/verify?token=not-a-real-token")
    check(r.status_code == 200 and "invalid or has expired" in r.text, f"bad token verify: {r.status_code}")

    # 9. Resend gives byte-identical responses for a real vs. fake email
    # (no account-existence oracle), but only actually emails the real one.
    r1 = client.post("/resend-verification", data={"email": "new.user@example.com"})
    # new.user@example.com is already verified by now, so this should NOT
    # send another email even though the account is real.
    check(len(sent_emails) == 1, "resend must not re-email an already-verified account")
    r2 = client.post("/resend-verification", data={"email": "totally-fake@example.com"})
    check(r1.text == r2.text, "resend response must not reveal whether the account exists")

    # 10. Unauthenticated WebSocket handshake is rejected with the
    # auth-failure close code (4401), not silently accepted. Needs a fresh
    # client — the shared `client` above is still carrying the session
    # cookie set by its earlier successful /login.
    from starlette.websockets import WebSocketDisconnect
    unauth_client = TestClient(app_module.app)
    try:
        with unauth_client.websocket_connect("/ws"):
            check(False, "expected the unauthenticated WS handshake to be rejected")
    except WebSocketDisconnect as exc:
        check(exc.code == 4401, f"WS close code: {exc.code}")

    # 11. Login rate limiting is namespaced separately from signup, so
    # hammering /login doesn't also lock out /signup from the same IP.
    for _ in range(5):
        client.post("/login", data={"email": "new.user@example.com", "password": "wrong"})
    r = client.post("/login", data={"email": "new.user@example.com", "password": "wrong"})
    check(r.status_code == 429, f"expected login rate limit to kick in: {r.status_code}")
    r = client.post(
        "/signup",
        data={
            "email": "unrelated@example.com",
            "password": "correct-horse-battery",
            "confirm_password": "correct-horse-battery",
            "registration_code": "let-me-in",
        },
    )
    check(r.status_code == 200, f"signup should be unaffected by the login lockout: {r.status_code}")

    # 12. A group-specific REGISTRATION_CODE_<GROUP> code works just like
    # the legacy one and tags the new account with that group's name.
    r = client.post(
        "/signup",
        data={
            "email": "cousin@example.com",
            "password": "correct-horse-battery",
            "confirm_password": "correct-horse-battery",
            "registration_code": "family-secret",
        },
    )
    check(r.status_code == 200 and "verification link" in r.text.lower(), f"family-group signup: {r.status_code} {r.text[:200]}")
    stored = next((u for u in user_store.list_users() if u.email == "cousin@example.com"), None)
    check(stored is not None and stored.group == "FAMILY", f"family-group signup should tag group 'FAMILY', got {stored.group if stored else None!r}")

    # --- Admin: view/delete accounts (/admin/users) ---
    # Step 11 above deliberately exhausted the login rate limit, and every
    # TestClient in this process reports the same client IP (the rate key
    # is IP-only), so it'd otherwise still be locked out here.
    app_module.auth._failed_attempts.clear()

    # 13. An account can be created directly (like manage_users.py add)
    # rather than through /signup — ADMIN_EMAILS (see env in
    # test_signup_flow.py) is what actually grants admin access, not how
    # the account was created.
    user_store.add_user("admin@example.com", "admin-password-1", verified=True)

    admin_client = TestClient(app_module.app)
    r = admin_client.post(
        "/login", data={"email": "admin@example.com", "password": "admin-password-1"}, follow_redirects=False
    )
    check(r.status_code == 303 and "stx_session" in admin_client.cookies, f"admin login: {r.status_code}")

    # 14. The admin can see every account on /admin/users.
    r = admin_client.get("/admin/users")
    check(r.status_code == 200, f"admin users page: {r.status_code}")
    check("new.user@example.com" in r.text and "cousin@example.com" in r.text, "admin users page missing known accounts")

    # 15. A logged-in but non-admin account gets a 403, not the page — the
    # shared `client` is still authenticated as new.user@example.com from
    # step 4, which isn't in ADMIN_EMAILS.
    r = client.get("/admin/users")
    check(r.status_code == 403, f"non-admin should get 403, got {r.status_code}")

    # 16. An unauthenticated visit redirects to /login rather than either
    # showing the page or a bare 401 (this is a browser-navigated page).
    r = unauth_client.get("/admin/users", follow_redirects=False)
    check(r.status_code == 303 and "/login" in r.headers.get("location", ""), f"unauthenticated admin visit: {r.status_code} -> {r.headers.get('location')}")

    # 17. Sorting by group puts blank-group accounts first (ascending) —
    # admin@example.com was created directly (no --group), so it has group
    # "" while cousin@example.com has "family" (signed up with the
    # REGISTRATION_CODE_FAMILY code) — "" sorts before any named group.
    r = admin_client.get("/admin/users?sort=group&dir=asc")
    check(r.status_code == 200, f"sort by group: {r.status_code}")
    check(r.text.index("admin@example.com") < r.text.index("cousin@example.com"), "blank-group accounts should sort before the 'family' group ascending")

    # 18. ...and dir=desc reverses that.
    r = admin_client.get("/admin/users?sort=group&dir=desc")
    check(r.status_code == 200, f"sort by group desc: {r.status_code}")
    check(r.text.index("admin@example.com") > r.text.index("cousin@example.com"), "'family' group should sort before blank-group accounts descending")

    # 19. An invalid sort field doesn't error, just falls back to email.
    r = admin_client.get("/admin/users?sort=not-a-real-column")
    check(r.status_code == 200, f"invalid sort field: {r.status_code}")

    # 20. CSV export lists every account, as a downloadable attachment, and
    # never leaks the password salt/hash columns.
    r = admin_client.get("/admin/users/export.csv")
    check(r.status_code == 200 and r.headers.get("content-type", "").startswith("text/csv"), f"csv export: {r.status_code} {r.headers.get('content-type')}")
    check("attachment" in r.headers.get("content-disposition", ""), "csv export should be a download, not inline")
    csv_text = r.text
    check("email,verified,group,created_at" in csv_text, f"csv header missing: {csv_text[:200]}")
    check("new.user@example.com" in csv_text and "cousin@example.com" in csv_text and "admin@example.com" in csv_text, "csv export missing known accounts")
    check("salt_hex" not in csv_text and "hash_hex" not in csv_text, "csv export must never include password salt/hash")
    # A non-admin can't hit the export endpoint either — it's under /admin.
    r = client.get("/admin/users/export.csv")
    check(r.status_code == 403, f"non-admin csv export should be forbidden: {r.status_code}")

    # 21. Admin can delete another account.
    r = admin_client.post("/admin/users/delete", data={"email": "cousin@example.com"}, follow_redirects=False)
    check(r.status_code == 303, f"delete cousin: {r.status_code}")
    check(not user_store.user_exists("cousin@example.com"), "cousin@example.com should be gone after admin delete")

    # 22. Admin can't delete their own account from this page (avoids an
    # accidental lockout) — specific error, account still exists.
    r = admin_client.post("/admin/users/delete", data={"email": "admin@example.com"})
    check(r.status_code == 400 and "own account" in r.text.lower(), f"self-delete guard: {r.status_code}")
    check(user_store.user_exists("admin@example.com"), "admin account should NOT have been deleted")

    # --- Forgot / reset password ---
    pw_client = TestClient(app_module.app)

    # 23. Requesting a reset for a real, existing account sends exactly
    # one email and gives the same generic response a nonexistent email
    # would (no account-existence oracle).
    before = len(sent_emails)
    r = pw_client.post("/forgot-password", data={"email": "New.User@Example.com"})
    check(r.status_code == 200 and "reset the" in r.text.lower(), f"forgot-password: {r.status_code} {r.text[:200]}")
    check(len(sent_emails) == before + 1, f"expected exactly 1 reset email, got {len(sent_emails) - before}")
    reset_subject, reset_body, reset_to = sent_emails[-1]
    check(reset_to == "new.user@example.com", f"reset email normalization: {reset_to!r}")

    # 24. A nonexistent email gets the identical response, no new email.
    before = len(sent_emails)
    r2 = pw_client.post("/forgot-password", data={"email": "nobody-at-all@example.com"})
    check(r2.status_code == 200 and r2.text == r.text, "forgot-password response must not reveal whether the account exists")
    check(len(sent_emails) == before, "no email should be sent for an account that doesn't exist")

    reset_url = next(line.strip() for line in reset_body.splitlines() if line.strip().startswith("http"))
    reset_token_quoted = reset_url.split("token=", 1)[1]
    # The email link carries a URL-quoted token (correct for a GET query
    # string, via quote() in web/app.py). Posting it as a FORM VALUE needs
    # the raw (unquoted) token instead — httpx form-encodes dict values for
    # transport, so handing it the already-quoted string would double-encode
    # it and the server would decode only one layer back off, leaving a
    # mangled token. unquote() here mirrors what a real browser submitting
    # the rendered <input type="hidden"> would send (that value is the raw
    # token, HTML-escaped, never URL-escaped — see render_reset_password_page).
    reset_token = unquote(reset_token_quoted)

    # 25. The reset link itself renders a working form.
    r = pw_client.get(f"/reset-password?token={reset_token_quoted}")
    check(r.status_code == 200 and 'action="/reset-password"' in r.text, f"reset-password page: {r.status_code}")

    # 26. Mismatched / short new passwords are rejected without consuming
    # the token (it's still usable afterward — checked by step 28 below).
    r = pw_client.post("/reset-password", data={"token": reset_token, "password": "new-password-1", "confirm_password": "different"})
    check(r.status_code == 400 and "match" in r.text.lower(), f"mismatched reset passwords: {r.status_code}")
    r = pw_client.post("/reset-password", data={"token": reset_token, "password": "short1", "confirm_password": "short1"})
    check(r.status_code == 400 and "8 characters" in r.text, f"short reset password: {r.status_code}")

    # 27. A garbage token shows the invalid-link page, not a 500.
    r = pw_client.get("/reset-password?token=not-a-real-token")
    check(r.status_code == 200 and "invalid or has expired" in r.text, f"bad reset token: {r.status_code}")
    r = pw_client.post("/reset-password", data={"token": "not-a-real-token", "password": "new-password-1", "confirm_password": "new-password-1"})
    check(r.status_code == 400 and "invalid or has expired" in r.text, f"bad reset token submit: {r.status_code}")

    # 28. A valid submission actually changes the password, and the old
    # one stops working while the new one logs in.
    r = pw_client.post("/reset-password", data={"token": reset_token, "password": "new-password-1", "confirm_password": "new-password-1"})
    check(r.status_code == 200 and "reset" in r.text.lower(), f"reset-password submit: {r.status_code} {r.text[:200]}")
    r = pw_client.post("/login", data={"email": "new.user@example.com", "password": "correct-horse-battery"})
    check(r.status_code == 401, f"old password should no longer work: {r.status_code}")
    r = pw_client.post("/login", data={"email": "new.user@example.com", "password": "new-password-1"}, follow_redirects=False)
    check(r.status_code == 303 and "stx_session" in pw_client.cookies, f"new password should work: {r.status_code}")

    # 29. Resetting the password of a never-verified account also
    # verifies it — clicking the link proves mailbox control the same way
    # a signup verification link does. unrelated@example.com signed up
    # earlier (step 11) but never clicked its verification link.
    check(not user_store.is_verified("unrelated@example.com"), "test setup assumption: unrelated@example.com should still be unverified")
    r = pw_client.post("/forgot-password", data={"email": "unrelated@example.com"})
    check(r.status_code == 200, f"forgot-password for unverified account: {r.status_code}")
    _, unrelated_body, _ = sent_emails[-1]
    unrelated_url = next(line.strip() for line in unrelated_body.splitlines() if line.strip().startswith("http"))
    unrelated_token = unquote(unrelated_url.split("token=", 1)[1])
    r = pw_client.post("/reset-password", data={"token": unrelated_token, "password": "another-new-pw-1", "confirm_password": "another-new-pw-1"})
    check(r.status_code == 200, f"reset for unverified account: {r.status_code}")
    check(user_store.is_verified("unrelated@example.com"), "resetting the password should have verified the account")
    r = pw_client.post("/login", data={"email": "unrelated@example.com", "password": "another-new-pw-1"}, follow_redirects=False)
    check(r.status_code == 303, f"login after reset-verification: {r.status_code}")

    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
