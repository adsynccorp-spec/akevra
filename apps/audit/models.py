from django.db import models

from apps.core.exceptions import HardDeleteNotAllowed
from apps.core.models import TimestampedModel, UUIDPrimaryKeyModel


class ImmutableQuerySet(models.QuerySet):
    def delete(self):
        raise HardDeleteNotAllowed("Audit history cannot be deleted.")

    def update(self, **kwargs):
        raise HardDeleteNotAllowed("Audit history cannot be updated.")


class ImmutableManager(models.Manager.from_queryset(ImmutableQuerySet)):
    pass


class AuditEvent(UUIDPrimaryKeyModel, TimestampedModel):
    """Append-only audit history. Written on create/update of domain records."""

    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor_identity = models.ForeignKey(
        "accounts.Identity",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor_user_account = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    action = models.CharField(max_length=64)
    entity_type = models.CharField(max_length=128)
    entity_id = models.CharField(max_length=64)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    objects = ImmutableManager()

    class Meta:
        db_table = "audit_event"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["organization", "created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise HardDeleteNotAllowed("Audit history cannot be updated.")
        return super().save(*args, **kwargs)

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteNotAllowed("Audit history cannot be deleted.")
