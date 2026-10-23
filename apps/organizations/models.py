from django.db import models

from apps.core.models import (
    OrganizationScopedModel,
    RecordStatus,
    SoftArchiveModel,
    TimestampedModel,
    UUIDPrimaryKeyModel,
)


class Organization(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel):
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=64, unique=True)
    status = models.CharField(max_length=32, default=RecordStatus.ACTIVE)
    # When on, supervisees must report other-agency hours/supervision (or confirm none) before a month is closed
    require_external_reporting = models.BooleanField(default=False)

    class Meta:
        db_table = "organization"

    def __str__(self):
        return self.name
