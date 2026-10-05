import uuid

from django.db import models

from apps.core.exceptions import HardDeleteNotAllowed


class UUIDPrimaryKeyModel(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    class Meta:
        abstract = True


class TimestampedModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class RecordStatus(models.TextChoices):
    ACTIVE = "active", "Active"
    SUPERSEDED = "superseded", "Superseded"
    ARCHIVED = "archived", "Archived"
    CONCLUDED = "concluded", "Concluded"


class NoDeleteQuerySet(models.QuerySet):
    def delete(self):
        raise HardDeleteNotAllowed(
            "Hard delete is not permitted. Archive or supersede the record instead."
        )


class NoDeleteManager(models.Manager.from_queryset(NoDeleteQuerySet)):
    pass


class SoftArchiveModel(models.Model):
    record_status = models.CharField(
        max_length=32,
        choices=RecordStatus.choices,
        default=RecordStatus.ACTIVE,
    )
    archived_at = models.DateTimeField(null=True, blank=True)

    objects = NoDeleteManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        raise HardDeleteNotAllowed(
            "Hard delete is not permitted. Archive or supersede the record instead."
        )


class OrganizationScopedModel(models.Model):
    organization = models.ForeignKey(
        "organizations.Organization",
        on_delete=models.PROTECT,
        related_name="%(class)ss",
    )

    class Meta:
        abstract = True
        indexes = [models.Index(fields=["organization"])]
