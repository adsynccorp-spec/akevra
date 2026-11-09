"""M7 Part A: concluding a relationship and a Supervisor leaving the organization.

A concluded relationship is never deleted or archived: it stays fully readable, with the conclusion
(reason, date, who recorded it) beside it, and no further edits to its setup."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.core.models import RecordStatus
from apps.supervision.models import (
    Appointment,
    AppointmentStatus,
    ConclusionReason,
    RelationshipConclusion,
    SupervisoryRelationship,
)


def conclude(relationship, actor, *, reason, concluded_on=None, notes=""):
    if relationship.status != RecordStatus.ACTIVE:
        raise ValidationError("This relationship is already concluded.")
    if reason not in ConclusionReason.values:
        raise ValidationError("Choose a reason for concluding the relationship.")
    notes = (notes or "").strip()
    if reason == ConclusionReason.OTHER and not notes:
        raise ValidationError("Describe the reason when choosing Other.")
    if isinstance(concluded_on, str):
        try:
            concluded_on = date.fromisoformat(concluded_on)
        except ValueError:
            raise ValidationError("Give the conclusion date as YYYY-MM-DD.") from None
    from apps.supervision.compliance import organization_today

    today = organization_today(relationship.organization)
    concluded_on = concluded_on or today
    if concluded_on < relationship.started_on:
        raise ValidationError("A relationship can't end before it started.")
    if concluded_on > today:
        raise ValidationError("The conclusion date can't be in the future.")
    with transaction.atomic():
        conclusion = RelationshipConclusion.objects.create(
            organization=relationship.organization, relationship=relationship, reason=reason,
            concluded_on=concluded_on, notes=notes, concluded_by=actor,
        )
        relationship.status = RecordStatus.CONCLUDED
        relationship.ended_on = concluded_on
        relationship.save(update_fields=["status", "ended_on", "updated_at"])
        # Upcoming appointments after the end are cancelled (kept on file with the reason)
        from apps.supervision import scheduling

        for appt in Appointment.objects.filter(relationship=relationship, record_status="active",
                                               status__in=scheduling.OPEN, starts_at__gte=timezone.now()):
            appt.status = AppointmentStatus.CANCELLED
            appt.cancel_reason = "Relationship concluded"
            appt.save()
            scheduling._event(appt, actor, "cancelled", reason="Relationship concluded")
    return conclusion


def depart(account, actor, *, notes=""):
    """A person leaves the organization: their sign-in to this workspace ends, their active
    relationships are concluded (reason: departed), and every record they wrote stays readable to the
    people authorized to see it."""
    if account.id == actor.id:
        raise ValidationError("You can't mark yourself as departed.")
    if account.record_status != RecordStatus.ACTIVE:
        raise ValidationError("This person has already left the organization.")
    concluded = []
    with transaction.atomic():
        for rel in SupervisoryRelationship.objects.filter(status=RecordStatus.ACTIVE).filter(supervisor=account):
            concluded.append(conclude(rel, actor, reason=ConclusionReason.SUPERVISOR_DEPARTED, notes=notes))
        for rel in SupervisoryRelationship.objects.filter(status=RecordStatus.ACTIVE).filter(supervisee=account):
            concluded.append(conclude(rel, actor, reason=ConclusionReason.SUPERVISEE_DEPARTED, notes=notes))
        account.record_status = RecordStatus.ARCHIVED
        account.save(update_fields=["record_status", "updated_at"])
        from apps.accounts.models import AuthSession

        AuthSession.objects.filter(user_account=account, revoked_at__isnull=True).update(revoked_at=timezone.now())
    return concluded
