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
    # IANA time zone that notification and email times are written in (the app itself shows the viewer's own clock)
    time_zone = models.CharField(max_length=64, default="America/New_York")
    # Opt-in: only when an Administrator turns this on is de-identified context sent to the external AI provider.
    # Off, suggestions come from the built-in rule generator and nothing leaves AKEVRA.
    ai_enabled = models.BooleanField(default=False)

    class Meta:
        db_table = "organization"

    def __str__(self):
        return self.name
