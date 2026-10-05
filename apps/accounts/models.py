import hashlib
import secrets
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.db import models
from django.utils import timezone

from apps.core.models import (
    OrganizationScopedModel,
    RecordStatus,
    SoftArchiveModel,
    TimestampedModel,
    UUIDPrimaryKeyModel,
)


class CredentialType(models.TextChoices):
    BCBA = "bcba", "BCBA"
    BCABA = "bcaba", "BCaBA"
    RBT = "rbt", "RBT"
    STUDENT_ANALYST = "student_analyst", "Student Analyst"


class IdentityManager(BaseUserManager):
    def create_user(self, email, password=None, **extra_fields):
        if not email:
            raise ValueError("Email is required")
        email = self.normalize_email(email).lower()
        identity = self.model(email=email, **extra_fields)
        identity.set_password(password)
        identity.save(using=self._db)
        return identity

    def create_superuser(self, email, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        return self.create_user(email, password, **extra_fields)


class Identity(UUIDPrimaryKeyModel, AbstractBaseUser, PermissionsMixin):
    """Login credential. One email identity may map to many UserAccounts (REQ-094)."""

    email = models.EmailField(unique=True)
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    mfa_enabled = models.BooleanField(default=False)
    mfa_secret_encrypted = models.TextField(blank=True, default="")
    failed_login_count = models.PositiveIntegerField(default=0)
    locked_until = models.DateTimeField(null=True, blank=True)
    # Forgot-password: HMAC of the emailed 6-digit code (never the code itself)
    password_reset_code_hash = models.CharField(max_length=64, blank=True, default="")
    password_reset_expires_at = models.DateTimeField(null=True, blank=True)
    password_reset_sent_at = models.DateTimeField(null=True, blank=True)
    password_reset_attempts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = IdentityManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    class Meta:
        db_table = "login_identity"

    def __str__(self):
        return self.email

    @property
    def is_locked(self):
        return bool(self.locked_until and self.locked_until > timezone.now())

    def register_failed_login(self):
        threshold = settings.AUTH_LOCKOUT_THRESHOLD
        self.failed_login_count += 1
        if self.failed_login_count >= threshold:
            self.locked_until = timezone.now() + timedelta(
                seconds=settings.AUTH_LOCKOUT_SECONDS
            )
        self.save(update_fields=["failed_login_count", "locked_until", "updated_at"])

    def reset_failed_logins(self):
        self.failed_login_count = 0
        self.locked_until = None
        self.save(update_fields=["failed_login_count", "locked_until", "updated_at"])


class UserAccount(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Organization-scoped person. Unique on (email, organization) per DEC-062 / REQ-094."""

    identity = models.ForeignKey(
        Identity,
        on_delete=models.PROTECT,
        related_name="user_accounts",
    )
    email = models.EmailField()
    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    credential_type = models.CharField(
        max_length=32,
        choices=CredentialType.choices,
        blank=True,
        default="",
    )
    status = models.CharField(max_length=32, default=RecordStatus.ACTIVE)

    class Meta:
        db_table = "user_account"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "email"],
                name="unique_user_email_per_organization",
            ),
            models.UniqueConstraint(
                fields=["organization", "identity"],
                name="unique_identity_per_organization",
            ),
        ]

    def __str__(self):
        return f"{self.email} @ {self.organization_id}"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()


class AuthSession(UUIDPrimaryKeyModel, TimestampedModel):
    identity = models.ForeignKey(
        Identity,
        on_delete=models.PROTECT,
        related_name="sessions",
    )
    user_account = models.ForeignKey(
        UserAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sessions",
    )
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sessions",
    )
    token_hash = models.CharField(max_length=64, unique=True)
    mfa_verified = models.BooleanField(default=False)
    workspace_selected = models.BooleanField(default=False)
    last_seen_at = models.DateTimeField(auto_now_add=True)
    absolute_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "auth_session"

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, identity, *, mfa_verified=False):
        raw = secrets.token_urlsafe(48)
        session = cls.objects.create(
            identity=identity,
            token_hash=cls.hash_token(raw),
            mfa_verified=mfa_verified,
            workspace_selected=False,
            absolute_expires_at=timezone.now()
            + timedelta(seconds=settings.AUTH_SESSION_ABSOLUTE_SECONDS),
        )
        session.raw_token = raw
        return session

    @property
    def is_revoked(self):
        return self.revoked_at is not None

    # Idle activity is recorded at most this often. The idle limit is minutes long, so a
    # short granularity changes nothing for the user.
    TOUCH_INTERVAL = timedelta(seconds=30)

    def touch(self):
        """Record activity without making concurrent requests wait on each other.

        Requests run in one transaction (ATOMIC_REQUESTS), so an UPDATE here holds the
        session row lock until the response is sent. A dashboard fires several requests
        at once with the same token; a plain save made them queue behind each other.
        Recent activity is skipped, and a row another request is already updating is
        skipped too (SKIP LOCKED) — that request records the same activity."""
        now = timezone.now()
        if now - self.last_seen_at < self.TOUCH_INTERVAL:
            return
        unlocked = AuthSession.objects.filter(pk=self.pk).select_for_update(skip_locked=True)
        AuthSession.objects.filter(pk__in=unlocked.values("pk")).update(last_seen_at=now, updated_at=now)
        self.last_seen_at = now

    def bind_workspace(self, user_account):
        self.user_account = user_account
        self.organization = user_account.organization
        self.workspace_selected = True
        self.save(
            update_fields=[
                "user_account",
                "organization",
                "workspace_selected",
                "updated_at",
            ]
        )

    def revoke(self):
        self.revoked_at = timezone.now()
        self.save(update_fields=["revoked_at", "updated_at"])

    def idle_expired(self):
        idle = timedelta(seconds=settings.AUTH_SESSION_IDLE_SECONDS)
        return timezone.now() - self.last_seen_at > idle

    def absolute_expired(self):
        return timezone.now() >= self.absolute_expires_at


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    ACCEPTED = "accepted", "Accepted"
    REVOKED = "revoked", "Revoked"


class Invitation(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """An Administrator's invitation for one person to join one Organization (RBAC-002).

    Only the SHA-256 of the invitation token is stored; the raw token lives only in the
    activation link. Supervisor/Supervisee are not invitable roles — they are evaluated
    per SupervisoryRelationship (DEC-057) — so an invitation carries the BACB credential
    and, optionally, an organization role.
    """

    email = models.EmailField()
    credential_type = models.CharField(
        max_length=32,
        choices=CredentialType.choices,
        blank=True,
        default="",
    )
    org_role = models.CharField(max_length=32, blank=True, default="")
    token_hash = models.CharField(max_length=64, unique=True)
    expires_at = models.DateTimeField()
    status = models.CharField(
        max_length=16,
        choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
    )
    invited_by = models.ForeignKey(
        UserAccount,
        on_delete=models.PROTECT,
        related_name="sent_invitations",
    )
    accepted_user_account = models.ForeignKey(
        UserAccount,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="accepted_invitations",
    )
    accepted_at = models.DateTimeField(null=True, blank=True)
    failed_attempts = models.PositiveIntegerField(default=0)
    # Pending TOTP secret generated during activation, moved to the Identity on success
    mfa_secret_encrypted = models.TextField(blank=True, default="")

    class Meta:
        db_table = "user_invitation"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "email"])]

    @property
    def is_expired(self):
        return timezone.now() >= self.expires_at

    @property
    def state(self):
        """pending | expired | accepted | revoked — 'expired' is derived, never stored."""
        if self.status == InvitationStatus.PENDING and self.is_expired:
            return "expired"
        return self.status
