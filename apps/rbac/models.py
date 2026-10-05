from django.db import models

from apps.core.models import (
    OrganizationScopedModel,
    SoftArchiveModel,
    TimestampedModel,
    UUIDPrimaryKeyModel,
)


class OrgRole(models.TextChoices):
    ADMINISTRATOR = "administrator", "Administrator"
    CLINICAL_DIRECTOR = "clinical_director", "Clinical Director"


class RelationshipRole(models.TextChoices):
    SUPERVISOR = "supervisor", "Supervisor"
    SUPERVISEE = "supervisee", "Supervisee"


class OrganizationRoleGrant(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Organization-scoped roles (Administrator, Clinical Director).

    Supervisor / Supervisee are NOT stored here — they are evaluated per
    SupervisoryRelationship (DEC-057).
    """

    user_account = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="org_role_grants",
    )
    role = models.CharField(max_length=32, choices=OrgRole.choices)

    class Meta:
        db_table = "organization_role_grant"
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "user_account", "role"],
                name="unique_org_role_grant",
            )
        ]

    def __str__(self):
        return f"{self.user_account_id}:{self.role}"
