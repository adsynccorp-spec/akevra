"""Reporting periods: the shared monthly service-hours record and the month's status.

- Service hours: one active record per supervisee and month, shared by every relationship
  (so concurrent supervisors never create separate denominators). No record = Missing; 0 hours
  is a confirmed zero with a reason. A correction supersedes the earlier record, which is kept.
- Period status: Open → Ready for Review → Verified/Closed, each change recorded with who and why.
  Closing stores a snapshot of the inputs, the result and the rule set; a closed month reports
  that snapshot. Reopening needs a reason. Compliance-bearing records can't be added to or changed
  in a closed month until it is reopened.
"""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.supervision.models import (
    SESSION_DERIVED_HOURS_TRACKS,
    CycleStatus,
    MonthlyCycle,
    MonthlyCycleEvent,
    ServiceHoursRecord,
    ServiceHoursSource,
    SupervisionTrack,
)


def ensure_open(cycle, what="records"):
    if cycle.status == CycleStatus.CLOSED:
        raise ValidationError(
            f"{date(cycle.year, cycle.month, 1):%B %Y} is verified and closed, so its {what} can't be changed. "
            "Reopen the month (with a reason) to make a correction."
        )


def _closed_months_for_supervisee(supervisee_id, year, month):
    return MonthlyCycle.objects.filter(
        relationship__supervisee_id=supervisee_id, year=year, month=month, status=CycleStatus.CLOSED
    )


# ---------------------------------------------------------------------------
# Shared service hours
# ---------------------------------------------------------------------------

def record_service_hours(relationship, actor, *, year, month, hours, zero_reason="", notes="", supporting_reference=""):
    """The supervisee states their service hours for a month (0 needs a reason). Replaces the
    active record by superseding it, so the original stays on file."""
    from apps.supervision.monthly import get_or_create_cycle, parse_decimal

    if relationship.supervision_track not in SESSION_DERIVED_HOURS_TRACKS:
        raise ValidationError(
            "Monthly service hours are recorded only on RBT or BCaBA ongoing supervision. "
            "Supervised Fieldwork hours are logged and verified instead."
        )
    if actor.id != relationship.supervisee_id:
        raise ValidationError("Monthly service hours are attested by the Supervisee.")
    year, month = int(year), int(month)
    if _closed_months_for_supervisee(relationship.supervisee_id, year, month).exists():
        raise ValidationError(
            f"{date(year, month, 1):%B %Y} is verified and closed, so its service hours can't be changed. "
            "Ask your supervisor to reopen the month for a correction."
        )
    value = parse_decimal(hours, "Service hours")
    with transaction.atomic():
        get_or_create_cycle(relationship, year, month)
        previous = ServiceHoursRecord.objects.select_for_update().filter(
            supervisee_id=relationship.supervisee_id, year=year, month=month, record_status="active"
        ).first()
        if previous is not None:
            previous.record_status = "superseded"
            previous.save(update_fields=["record_status", "updated_at"])
        record = ServiceHoursRecord(
            organization=relationship.organization,
            supervisee=relationship.supervisee,
            year=year,
            month=month,
            hours=value,
            zero_reason=zero_reason or "",
            notes=notes or "",
            supporting_reference=supporting_reference or "",
            source_type=ServiceHoursSource.SELF_ATTESTED,
            entered_by=actor,
            supersedes=previous,
        )
        record.save()
    return record


def review_service_hours(record, actor):
    if record.record_status != "active":
        raise ValidationError("Only the current service-hours record can be reviewed.")
    record.reviewed_by = actor
    record.reviewed_at = timezone.now()
    record.save(update_fields=["reviewed_by", "reviewed_at", "updated_at"])
    return record


def serialize_service_hours(record):
    if record is None:
        return None
    name = lambda a: None if a is None else {"id": str(a.id), "name": a.full_name}  # noqa: E731
    return {
        "id": str(record.id),
        "year": record.year,
        "month": record.month,
        "hours": float(record.hours),
        "confirmed_zero": record.hours == 0,
        "zero_reason": record.zero_reason,
        "zero_reason_label": record.get_zero_reason_display() if record.zero_reason else "",
        "notes": record.notes,
        "status": record.record_status,
        "source_type": record.source_type,
        "source_system": record.source_system,
        "source_reference": record.source_reference,
        "supporting_reference": record.supporting_reference,
        "entered_by": name(record.entered_by),
        "entered_at": record.created_at.isoformat(),
        "reviewed_by": name(record.reviewed_by),
        "reviewed_at": record.reviewed_at.isoformat() if record.reviewed_at else None,
        "supersedes_id": str(record.supersedes_id) if record.supersedes_id else None,
        # Kept so the existing UI keeps working
        "attested_by": name(record.entered_by),
        "attested_on": timezone.localtime(record.created_at).date().isoformat(),
    }


# ---------------------------------------------------------------------------
# Period status
# ---------------------------------------------------------------------------

ALLOWED = {
    CycleStatus.OPEN: {CycleStatus.READY_FOR_REVIEW, CycleStatus.CLOSED},
    CycleStatus.READY_FOR_REVIEW: {CycleStatus.OPEN, CycleStatus.CLOSED},
    CycleStatus.CLOSED: {CycleStatus.OPEN},
}


def _snapshot(cycle, actor):
    """Inputs, result and rule set as they stand at closing."""
    from apps.supervision.compliance import cycle_compliance

    result = cycle_compliance(cycle, live=True)
    rel = cycle.relationship
    inputs = {
        "session_ids": [str(pk) for pk in cycle.sessions.filter(record_status="active", status="completed").values_list("id", flat=True)],
        "hours_entry_ids": [str(pk) for pk in cycle.hours_entries.filter(record_status="active", status="verified").values_list("id", flat=True)],
    }
    if rel.supervision_track in SESSION_DERIVED_HOURS_TRACKS:
        record = ServiceHoursRecord.objects.filter(supervisee_id=rel.supervisee_id, year=cycle.year, month=cycle.month,
                                                   record_status="active").first()
        inputs["service_hours_record_id"] = str(record.id) if record else None
    return {
        "result": result,
        "inputs": inputs,
        "rule_set_id": (result.get("rules") or {}).get("rule_set_id"),
        "rule_set_name": (result.get("rules") or {}).get("name"),
        "closed_at": timezone.now().isoformat(),
        "closed_by": actor.full_name,
    }


def change_status(cycle, actor, to_status, reason=""):
    current = cycle.status
    if to_status not in CycleStatus.values:
        raise ValidationError("Unknown period status.")
    if to_status == current:
        raise ValidationError(f"This month is already {CycleStatus(current).label}.")
    if to_status not in ALLOWED.get(current, set()):
        raise ValidationError(f"A month can't go from {CycleStatus(current).label} to {CycleStatus(to_status).label}.")
    reason = (reason or "").strip()
    if to_status == CycleStatus.CLOSED:
        today = timezone.localdate()
        if (cycle.year, cycle.month) >= (today.year, today.month):
            raise ValidationError("A month can be verified and closed only after it has ended.")
        rel = cycle.relationship
        if rel.supervision_track not in (*SESSION_DERIVED_HOURS_TRACKS, SupervisionTrack.SUPERVISED_FIELDWORK):
            raise ValidationError("Only compliance-bearing months are verified and closed.")
    if current == CycleStatus.CLOSED and not reason:
        raise ValidationError("Give a reason for reopening a verified month.")
    with transaction.atomic():
        if to_status == CycleStatus.CLOSED:
            cycle.closed_snapshot = _snapshot(cycle, actor)
            cycle.closed_at = timezone.now()
            cycle.closed_by = actor
        elif current == CycleStatus.CLOSED:
            # The previous snapshot stays in the row audit; the month is derived live again
            cycle.closed_snapshot = None
            cycle.closed_at = None
            cycle.closed_by = None
        cycle.status = to_status
        cycle.save()
        MonthlyCycleEvent.objects.create(
            organization=cycle.organization, cycle=cycle, from_status=current, to_status=to_status, actor=actor, reason=reason,
        )
    return cycle


def serialize_events(cycle):
    return [
        {
            "from_status": e.from_status,
            "to_status": e.to_status,
            "actor": {"id": str(e.actor_id), "name": e.actor.full_name},
            "reason": e.reason,
            "at": e.created_at.isoformat(),
        }
        for e in cycle.events.select_related("actor").all()
    ]
