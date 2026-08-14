"""Email alerting via plain SMTP.

No connected MCP mail connector here supports unattended sending (the Gmail
connector, if connected, only creates drafts a human has to send manually),
so alerts go out over SMTP using credentials you provide in .env.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from config import settings

logger = logging.getLogger("alerts.email_alert")


def send_email_alert(subject: str, body: str, to_address: str | None = None) -> bool:
    """Send a plain-text alert email. Returns True if sent, False if
    skipped/failed. Never raises — alerting must not crash the monitor loop."""
    if not settings.has_smtp_credentials():
        logger.warning(
            "SMTP credentials not set (SMTP_HOST/SMTP_USERNAME/SMTP_PASSWORD) "
            "— skipping email alert: %s | %s", subject, body
        )
        return False

    to = to_address or settings.alert_email_to
    if not to:
        logger.warning("No ALERT_EMAIL_TO configured — skipping email alert.")
        return False

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = settings.smtp_from_address or settings.smtp_username
    msg["To"] = to
    msg.set_content(body)

    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as server:
            server.starttls()
            server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(msg)
        logger.info("Alert email sent to %s: %s", to, subject)
        return True
    except Exception:
        logger.exception("Failed to send alert email")
        return False
