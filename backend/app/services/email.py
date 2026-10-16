"""Outgoing email over plain SMTP (stdlib ``smtplib``).

Provider agnostic; the settings default to Resend's SMTP relay (SMTPS on 465,
user ``resend``, password ``RESEND_API_KEY``). Never logs message bodies.
"""

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from app.core.config import Settings

logger = logging.getLogger("app.email")


def build_message(settings: Settings, to: str, subject: str, text: str, html: str) -> EmailMessage:
    """A ``multipart/alternative`` (text/plain + text/html) message from
    ``EMAIL_FROM`` to one recipient; callers may add parts before sending."""
    msg = EmailMessage()
    msg["From"] = settings.email_from
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=settings.email_from.rsplit("@", 1)[-1].strip("> "))
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_message(settings: Settings, msg: EmailMessage) -> None:
    """Deliver ``msg`` over SMTPS (raises on any SMTP / network error)."""
    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, context=context, timeout=20) as s:
        s.login(settings.smtp_user, settings.resend_api_key)
        s.send_message(msg)


def send_email(settings: Settings, to: str, subject: str, text: str, html: str) -> None:
    send_message(settings, build_message(settings, to, subject, text, html))
