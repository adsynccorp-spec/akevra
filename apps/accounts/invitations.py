"""Invitation-based account activation (RBAC-002: Administrator creates User Accounts).

Flow:
  1. Administrator creates an invitation  -> activation link emailed (72h by default).
  2. Invitee opens the link               -> GET details (public, by token).
  3. Invitee submits name + password      -> verify: password checked, TOTP secret issued.
  4. Invitee submits the authenticator code -> activate: Identity/UserAccount created,
     MFA enabled, session issued (same payload as login).

A person who already has an AKEVRA login (same email in another Organization, DEC-062)
keeps their existing password and MFA; the invitation only adds a UserAccount.
"""

import hashlib
import re
import secrets
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.accounts.mfa import (
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    new_totp_secret,
    totp_uri,
    verify_totp,
)
from apps.accounts.models import (
    AuthSession,
    CredentialType,
    Identity,
    Invitation,
    InvitationStatus,
    UserAccount,
)
from apps.accounts.passwords import PasswordPolicyError, check_new_password
from apps.accounts.services import complete_post_mfa
from apps.audit.services import write_audit
from apps.core import rls
from apps.core.email import send_email
from apps.core.exceptions import AccountLocked
from apps.rbac.models import OrganizationRoleGrant, OrgRole


class InvitationError(Exception):
    """A user-facing validation problem (HTTP 400)."""


class InvitationUnavailable(Exception):
    """The invitation cannot be used: expired, accepted, revoked, or unknown (HTTP 410)."""

    def __init__(self, invitation=None):
        self.invitation = invitation
        super().__init__("This invitation is no longer valid.")


class InvalidCode(Exception):
    """Wrong authenticator code (HTTP 400)."""


def _hash(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _new_token():
    raw = secrets.token_urlsafe(32)
    return raw, _hash(raw)


def _expiry():
    return timezone.now() + timedelta(hours=settings.INVITATION_TTL_HOURS)


def activation_url(raw_token):
    return f"{settings.FRONTEND_URL.rstrip('/')}/activate/{raw_token}"


def role_label(credential_type, org_role):
    """What the invitee sees as their 'Invited Role'."""
    if org_role:
        return OrgRole(org_role).label
    if credential_type:
        return CredentialType(credential_type).label
    return "Member"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------
def serialize_invitation(invitation, raw_token=None):
    """Administrator-facing view (never includes the token hash)."""
    data = {
        "id": str(invitation.id),
        "email": invitation.email,
        "credential_type": invitation.credential_type,
        "org_role": invitation.org_role,
        "role_label": role_label(invitation.credential_type, invitation.org_role),
        "state": invitation.state,
        "expires_at": invitation.expires_at.isoformat(),
        "created_at": invitation.created_at.isoformat(),
        "accepted_at": invitation.accepted_at.isoformat() if invitation.accepted_at else None,
        "invited_by": invitation.invited_by.full_name,
    }
    if raw_token:
        data["activation_url"] = activation_url(raw_token)
    return data


def serialize_public(invitation):
    """Invitee-facing view, returned by token."""
    identity = Identity.objects.filter(email=invitation.email).first()
    return {
        "email": invitation.email,
        "organization": {"name": invitation.organization.name, "slug": invitation.organization.slug},
        "credential_type": invitation.credential_type,
        "org_role": invitation.org_role,
        "role_label": role_label(invitation.credential_type, invitation.org_role),
        "state": invitation.state,
        "expires_at": invitation.expires_at.isoformat(),
        "ttl_hours": settings.INVITATION_TTL_HOURS,
        "invited_by": {"name": invitation.invited_by.full_name, "email": invitation.invited_by.email},
        # An existing login keeps its password and authenticator (DEC-062)
        "existing_account": identity is not None,
    }


# ---------------------------------------------------------------------------
# Administrator actions
# ---------------------------------------------------------------------------
def _send_email(invitation, raw_token):
    """Returns {"email_sent", "email_error"} for the Administrator; the link works either way."""
    sent, error = send_email(
        subject=f"You've been invited to join {invitation.organization.name} on AKEVRA",
        message=(
            f"{invitation.invited_by.full_name} has invited you to join "
            f"{invitation.organization.name} on AKEVRA.\n\n"
            f"Activate your account: {activation_url(raw_token)}\n\n"
            f"This link expires in {settings.INVITATION_TTL_HOURS} hours."
        ),
        to=invitation.email,
    )
    return {"email_sent": sent, "email_error": error}


def create_invitation(*, inviter, email, credential_type="", org_role="", request=None):
    email = (email or "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise InvitationError("Enter a valid email address.")
    if credential_type not in ("", *CredentialType.values):
        raise InvitationError("Unknown credential type.")
    if org_role not in ("", *OrgRole.values):
        raise InvitationError("Unknown organization role.")

    organization = inviter.organization
    if UserAccount.objects.filter(organization=organization, email=email).exists():
        raise InvitationError("This person already has an account in your organization.")

    # A new invitation supersedes any earlier pending one for the same email
    Invitation.objects.filter(
        organization=organization, email=email, status=InvitationStatus.PENDING
    ).update(status=InvitationStatus.REVOKED)

    raw, token_hash = _new_token()
    invitation = Invitation.objects.create(
        organization=organization,
        email=email,
        credential_type=credential_type,
        org_role=org_role,
        token_hash=token_hash,
        expires_at=_expiry(),
        invited_by=inviter,
    )
    delivery = _send_email(invitation, raw)
    write_audit(
        action="invitation.created",
        entity_type="user_invitation",
        entity_id=invitation.id,
        request=request,
        metadata={"email": email, "credential_type": credential_type, "org_role": org_role},
    )
    return invitation, raw, delivery


def resend_invitation(invitation, request=None):
    if invitation.status != InvitationStatus.PENDING:
        raise InvitationError("Only pending invitations can be resent.")
    raw, token_hash = _new_token()
    invitation.token_hash = token_hash
    invitation.expires_at = _expiry()
    invitation.failed_attempts = 0
    invitation.mfa_secret_encrypted = ""
    invitation.save(update_fields=[
        "token_hash", "expires_at", "failed_attempts", "mfa_secret_encrypted", "updated_at",
    ])
    delivery = _send_email(invitation, raw)
    write_audit(action="invitation.resent", entity_type="user_invitation", entity_id=invitation.id, request=request)
    return raw, delivery


def revoke_invitation(invitation, request=None):
    if invitation.status != InvitationStatus.PENDING:
        raise InvitationError("Only pending invitations can be revoked.")
    invitation.status = InvitationStatus.REVOKED
    invitation.mfa_secret_encrypted = ""
    invitation.save(update_fields=["status", "mfa_secret_encrypted", "updated_at"])
    write_audit(action="invitation.revoked", entity_type="user_invitation", entity_id=invitation.id, request=request)


# ---------------------------------------------------------------------------
# Invitee actions (public, token-authenticated). The auth-bootstrap RLS bypass is
# used the same way login uses it: the invitee has no session or tenant yet.
# ---------------------------------------------------------------------------
def find_invitation(raw_token, *, for_update=False):
    """Return the invitation for a token, or None. Caller must be inside rls_bypass()."""
    qs = Invitation.objects.select_related("organization", "invited_by")
    if for_update:
        qs = qs.select_for_update(of=("self",))
    return qs.filter(token_hash=_hash(raw_token or "")).first()


def _usable(raw_token, *, for_update=False):
    invitation = find_invitation(raw_token, for_update=for_update)
    if invitation is None or invitation.state != InvitationStatus.PENDING:
        raise InvitationUnavailable(invitation)
    return invitation



def _split_name(full_name):
    parts = (full_name or "").split()
    if not parts:
        raise InvitationError("Full name is required.")
    return parts[0][:150], " ".join(parts[1:])[:150]


def _check_credentials(invitation, full_name, password):
    """Validate the invitee's input. Returns the existing Identity, or None for a new login."""
    _split_name(full_name)
    identity = Identity.objects.filter(email=invitation.email).first()
    if identity is None:
        try:
            check_new_password(password or "", invitation.email)
        except PasswordPolicyError as exc:
            raise InvitationError(str(exc)) from exc
        return None
    if identity.is_locked:
        raise AccountLocked(identity.locked_until)
    if not identity.is_active or not identity.check_password(password or ""):
        identity.register_failed_login()
        if identity.is_locked:
            raise AccountLocked(identity.locked_until)
        raise InvitationError("That password doesn't match your existing AKEVRA account.")
    return identity


def verify_invitation(raw_token, full_name, password):
    """Step 1: check name/password and return the authenticator setup for step 2."""
    with rls.rls_bypass():
        invitation = _usable(raw_token, for_update=True)
        identity = _check_credentials(invitation, full_name, password)
        if identity is not None and identity.mfa_enabled:
            # Existing login confirms with the authenticator they already use
            return {"mfa_setup": None, "existing_account": True}

        secret = new_totp_secret()
        invitation.mfa_secret_encrypted = encrypt_mfa_secret(secret)
        invitation.save(update_fields=["mfa_secret_encrypted", "updated_at"])
        return {
            "mfa_setup": {"secret": secret, "otpauth_uri": totp_uri(invitation.email, secret)},
            "existing_account": identity is not None,
        }


def activate_invitation(raw_token, full_name, password, code):
    """Step 2: confirm the authenticator code and create the account. Returns a login payload."""
    with rls.rls_bypass():
        invitation = _usable(raw_token, for_update=True)
        identity = _check_credentials(invitation, full_name, password)

        if identity is not None and identity.mfa_enabled:
            secret_encrypted = identity.mfa_secret_encrypted
        else:
            secret_encrypted = invitation.mfa_secret_encrypted
        if not secret_encrypted:
            raise InvitationError("Set up your authenticator first.")

        if not verify_totp(decrypt_mfa_secret(secret_encrypted), code or ""):
            invitation.failed_attempts += 1
            fields = ["failed_attempts", "updated_at"]
            if invitation.failed_attempts >= settings.AUTH_LOCKOUT_THRESHOLD:
                invitation.status = InvitationStatus.REVOKED
                invitation.mfa_secret_encrypted = ""
                fields += ["status", "mfa_secret_encrypted"]
            invitation.save(update_fields=fields)
            if invitation.status == InvitationStatus.REVOKED:
                raise InvitationUnavailable(invitation)
            raise InvalidCode("Invalid authentication code.")

        if identity is None:
            identity = Identity.objects.create_user(email=invitation.email, password=password)
        if not identity.mfa_enabled:
            identity.mfa_secret_encrypted = secret_encrypted
            identity.mfa_enabled = True
            identity.save(update_fields=["mfa_secret_encrypted", "mfa_enabled", "updated_at"])

        first_name, last_name = _split_name(full_name)
        account = UserAccount.objects.create(
            identity=identity,
            organization=invitation.organization,
            email=invitation.email,
            first_name=first_name,
            last_name=last_name,
            credential_type=invitation.credential_type,
        )
        if invitation.org_role:
            OrganizationRoleGrant.objects.create(
                organization=invitation.organization,
                user_account=account,
                role=invitation.org_role,
            )

        invitation.status = InvitationStatus.ACCEPTED
        invitation.accepted_at = timezone.now()
        invitation.accepted_user_account = account
        invitation.mfa_secret_encrypted = ""
        invitation.save(update_fields=[
            "status", "accepted_at", "accepted_user_account", "mfa_secret_encrypted", "updated_at",
        ])
        write_audit(
            action="invitation.accepted",
            entity_type="user_invitation",
            entity_id=invitation.id,
            organization=invitation.organization,
            metadata={"user_account_id": str(account.id)},
        )

        # Signed in straight away: password + authenticator were just verified
        session = AuthSession.issue(identity, mfa_verified=True)
        return complete_post_mfa(session)
