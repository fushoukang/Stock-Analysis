"""End-to-end test of the signup -> email-verification -> login flow
against a real FastAPI app instance.

Runs tests/_signup_flow_runner.py in an ISOLATED SUBPROCESS rather than
importing web.app in this pytest process — see that file's docstring for
why: web/app.py builds its BarStore from config.settings at *import* time,
and this shared pytest process may already have a config.settings singleton
pointed at the real project data_store.db by the time this test runs. A
subprocess with its own environment (DB_PATH/USERS_PATH/WATCHLISTS_PATH all
pointed at pytest's tmp_path) sidesteps that risk entirely.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RUNNER_SCRIPT = Path(__file__).resolve().parent / "_signup_flow_runner.py"


def test_signup_verify_login_flow_end_to_end(tmp_path):
    env = dict(os.environ)
    env.update(
        USERS_PATH=str(tmp_path / "users.json"),
        DB_PATH=str(tmp_path / "test.db"),
        WATCHLISTS_PATH=str(tmp_path / "watchlists.json"),
        SESSION_SECRET_KEY="test-integration-secret",
        REGISTRATION_CODE="let-me-in",
        REGISTRATION_CODE_FAMILY="family-secret",
        ADMIN_EMAILS="admin@example.com",
        # Fake but present — send_email_alert itself gets monkeypatched
        # inside the runner script, so no real SMTP connection is ever
        # attempted; these just need to make has_smtp_credentials() True.
        SMTP_HOST="smtp.example.com",
        SMTP_USERNAME="bot@example.com",
        SMTP_PASSWORD="smtp-pw",
    )
    # Running the script by path (not `-m`) puts the script's own directory
    # on sys.path[0], not the project root — PYTHONPATH is what lets
    # "import web.app"/"import config" etc. resolve inside the subprocess.
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(PROJECT_ROOT) + (os.pathsep + existing_pythonpath if existing_pythonpath else "")

    result = subprocess.run(
        [sys.executable, str(RUNNER_SCRIPT)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0 and result.stdout.strip().endswith("OK"), (
        f"signup flow runner failed (exit {result.returncode}):\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
