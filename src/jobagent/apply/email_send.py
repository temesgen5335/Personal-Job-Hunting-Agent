"""Send an application email via SMTP (stdlib). Optionally attach the CV file.

The SMTP connection is injectable so tests can pass a fake without a real server.
"""

from __future__ import annotations

import mimetypes
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path


def build_message(
    from_addr: str, to_addr: str, subject: str, body: str, attachment_path: str | None = None
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content(body)
    if attachment_path and Path(attachment_path).exists():
        p = Path(attachment_path)
        ctype, _ = mimetypes.guess_type(p.name)
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(p.read_bytes(), maintype=maintype, subtype=subtype, filename=p.name)
    return msg


def send_email(
    settings,
    to_addr: str,
    subject: str,
    body: str,
    attachment_path: str | None = None,
    smtp: smtplib.SMTP | None = None,
) -> None:
    if not (settings.smtp_host and settings.apply_from_email):
        raise ValueError("SMTP_HOST and APPLY_FROM_EMAIL must be set to send email")
    msg = build_message(settings.apply_from_email, to_addr, subject, body, attachment_path)
    owns = smtp is None
    server = smtp or smtplib.SMTP(settings.smtp_host, settings.smtp_port)
    try:
        server.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    finally:
        if owns:
            server.quit()
