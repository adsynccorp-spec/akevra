"""Outgoing email.

SMTP works through Django's normal EMAIL_* settings. Hosts such as Railway block outbound
SMTP on smaller plans, so Brevo's HTTPS API (port 443) is available as a drop-in backend:

    EMAIL_BACKEND=apps.core.email.BrevoEmailBackend
    BREVO_API_KEY=<key from Brevo → SMTP & API → API Keys>
"""

import json
import logging
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.conf import settings
from django.core.mail import send_mail
from django.core.mail.backends.base import BaseEmailBackend

logger = logging.getLogger(__name__)


class BrevoEmailBackend(BaseEmailBackend):
    API_URL = "https://api.brevo.com/v3/smtp/email"

    def send_messages(self, email_messages):
        sent = 0
        for message in email_messages:
            try:
                self._send(message)
                sent += 1
            except Exception:
                if not self.fail_silently:
                    raise
        return sent

    def _send(self, message):
        if not settings.BREVO_API_KEY:
            raise RuntimeError("BREVO_API_KEY is not set.")
        name, address = parseaddr(message.from_email)
        payload = {
            "sender": {"name": name or "AKEVRA", "email": address},
            "to": [{"email": to} for to in message.to],
            "subject": message.subject,
            "textContent": message.body,
        }
        request = urllib.request.Request(
            self.API_URL,
            data=json.dumps(payload).encode(),
            method="POST",
            headers={
                "api-key": settings.BREVO_API_KEY,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=settings.EMAIL_TIMEOUT):
                pass
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:200]
            raise RuntimeError(f"Brevo rejected the email ({exc.code}): {detail}") from exc


def send_email(*, subject, message, to):
    """Send one plain-text email. Never raises; returns (sent, error) where `error` is a
    short reason an Administrator can act on."""
    if settings.EMAIL_BACKEND.endswith("console.EmailBackend"):
        logger.warning("EMAIL_BACKEND is the console backend; email to %s not delivered", to)
        return False, "Email is not configured on the server (EMAIL_BACKEND is not set)."
    try:
        send_mail(subject=subject, message=message, from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[to])
    except Exception as exc:
        logger.exception("Could not send email to %s", to)
        return False, f"Email could not be sent: {exc}"
    return True, None
