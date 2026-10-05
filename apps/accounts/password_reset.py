"""Forgot / reset password with a 6-digit code emailed to the login address.

Flow:
  1. request -> a code is emailed (PASSWORD_RESET_TTL_MINUTES). The answer is the same whether
                or not the email has an account, so the endpoint cannot be used to find users.
  2. verify  -> checks the code so the UI can move on to the new-password screen.
  3. reset   -> code + new password: password changed, lockout cleared, every session signed out.

Wrong codes count toward AUTH_LOCKOUT_THRESHOLD; after that the code is burned.
MFA is untouched — signing in afterwards still needs the authenticator code.
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.models import AuthSession, Identity
from apps.accounts.passwords import check_new_password
from apps.audit.services import write_audit
from apps.core import rls
from apps.core.email import send_email

RESEND_COOLDOWN = timedelta(seconds=60)
INVALID_CODE = "Invalid or expired code. Check the latest email or request a new code."
RESET_FIELDS = [
    "password_reset_code_hash",
    "password_reset_expires_at",
    "password_reset_sent_at",
    "password_reset_attempts",
    "updated_at",
]


class ResetError(Exception):
    """Wrong / expired code or a password that fails the policy (HTTP 400)."""


def _hash(identity, code):
    message = f"{identity.id}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def _identity(email):
    """Look up the login for an unauthenticated request, then scope RLS to it."""
    email = (email or "").strip().lower()
    with rls.rls_bypass():
        identity = Identity.objects.filter(email=email, is_active=True).first()
    if identity is not None:
        rls.set_tenant_context(identity_id=identity.id)
    return identity


def request_reset(email):
    identity = _identity(email)
    if identity is None:
        return
    now = timezone.now()
    if identity.password_reset_sent_at and now - identity.password_reset_sent_at < RESEND_COOLDOWN:
        return  # the code sent moments ago is still valid

    code = f"{secrets.randbelow(10**6):06d}"
    identity.password_reset_code_hash = _hash(identity, code)
    identity.password_reset_expires_at = now + timedelta(minutes=settings.PASSWORD_RESET_TTL_MINUTES)
    identity.password_reset_sent_at = now
    identity.password_reset_attempts = 0
    identity.save(update_fields=RESET_FIELDS)

    sent, _ = send_email(
        subject="Your AKEVRA password reset code",
        message=(
            f"Your AKEVRA verification code is: {code}\n\n"
            f"It expires in {settings.PASSWORD_RESET_TTL_MINUTES} minutes. "
            "If you didn't ask to reset your password, you can ignore this email."
        ),
        to=identity.email,
    )
    write_audit(
        action="password_reset.requested",
        entity_type="login_identity",
        entity_id=identity.id,
        metadata={"email_sent": sent},
    )


def _check_code(identity, code):
    if (
        identity is None
        or not identity.password_reset_code_hash
        or identity.password_reset_expires_at < timezone.now()
    ):
        raise ResetError(INVALID_CODE)
    if hmac.compare_digest(identity.password_reset_code_hash, _hash(identity, code or "")):
        return

    identity.password_reset_attempts += 1
    if identity.password_reset_attempts >= settings.AUTH_LOCKOUT_THRESHOLD:
        identity.password_reset_code_hash = ""
        identity.save(update_fields=RESET_FIELDS)
        write_audit(action="password_reset.code_burned", entity_type="login_identity", entity_id=identity.id)
        raise ResetError("Too many incorrect attempts. Request a new code.")
    identity.save(update_fields=RESET_FIELDS)
    raise ResetError(INVALID_CODE)


def verify_code(email, code):
    _check_code(_identity(email), code)


def reset_password(email, code, password):
    identity = _identity(email)
    _check_code(identity, code)
    check_new_password(password or "", identity.email)

    now = timezone.now()
    identity.set_password(password)
    identity.password_reset_code_hash = ""
    identity.password_reset_expires_at = None
    identity.password_reset_attempts = 0
    identity.failed_login_count = 0
    identity.locked_until = None
    identity.save(update_fields=[*RESET_FIELDS, "password", "failed_login_count", "locked_until"])

    # Anyone signed in with the old password is signed out
    AuthSession.objects.filter(identity=identity, revoked_at__isnull=True).update(revoked_at=now, updated_at=now)
    write_audit(action="password_reset.completed", entity_type="login_identity", entity_id=identity.id)

    send_email(
        subject="Your AKEVRA password was changed",
        message=(
            "The password for your AKEVRA account was just changed.\n\n"
            "If this wasn't you, contact your organization administrator immediately."
        ),
        to=identity.email,
    )
