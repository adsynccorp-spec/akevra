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
    ExternalSupervisionRecord,
    MonthlyCycle,
    MonthlyCycleEvent,
    ServiceHoursPeriodEntry,
    ServiceHoursRecord,
    ServiceHoursReview,
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

def record_service_hours(relationship, actor, *, year, month, hours, zero_reason="", notes="", supporting_reference="",
                         scope="internal", external_organization=""):
    """The supervisee states their service hours for a month (0 needs a reason): for this
    organization (`internal`) or self-reported for another agency (`external`, named). Replaces the
    active record for that source by superseding it, so the original stays on file."""
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
    value = parse_decimal(hours if scope != "external_none" else "0", "Service hours")
    with transaction.atomic():
        get_or_create_cycle(relationship, year, month)
        # The shared month appears on every active relationship the supervisee has on these pathways
        from calendar import monthrange

        from apps.supervision.models import SupervisoryRelationship
        month_end = date(year, month, monthrange(year, month)[1])
        for other in SupervisoryRelationship.objects.filter(
            supervisee_id=relationship.supervisee_id, supervision_track__in=SESSION_DERIVED_HOURS_TRACKS, status="active",
            started_on__lte=month_end,
        ).exclude(pk=relationship.pk):
            get_or_create_cycle(other, year, month)
        external_organization = (external_organization or "").strip() if scope == "external" else ""
        others = ServiceHoursRecord.objects.filter(supervisee_id=relationship.supervisee_id, year=year, month=month, record_status="active")
        if scope == "external_none" and others.filter(scope="external").exists():
            raise ValidationError("Other-agency hours are already reported for this month, so 'no other agency' can't be confirmed.")
        if scope == "external":
            # Reporting another agency replaces an earlier "no other agency" confirmation (kept, superseded)
            others.filter(scope="external_none").update(record_status="superseded", updated_at=timezone.now())
        previous = ServiceHoursRecord.objects.select_for_update().filter(
            supervisee_id=relationship.supervisee_id, year=year, month=month, record_status="active",
            scope=scope, external_organization=external_organization,
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
            scope=scope,
            external_organization=external_organization,
            zero_reason=zero_reason or "",
            notes=notes or "",
            supporting_reference=supporting_reference or "",
            source_type=ServiceHoursSource.SELF_ATTESTED,
            entered_by=actor,
            supersedes=previous,
        )
        record.save()
    return record


def review_service_hours(record, actor, relationship=None):
    """Each supervisor reviews the shared record for their own relationship; the review is theirs."""
    if record.record_status != "active":
        raise ValidationError("Only the current service-hours record can be reviewed.")
    if relationship is not None and (relationship.supervisee_id != record.supervisee_id or relationship.supervisor_id != actor.id):
        raise ValidationError("Review the hours from one of your own relationships with this supervisee.")
    if ServiceHoursReview.objects.filter(record=record, reviewer=actor, record_status="active").exists():
        raise ValidationError("You have already reviewed these hours.")
    ServiceHoursReview.objects.create(organization=record.organization, record=record, reviewer=actor, relationship=relationship)
    record.reviewed_by = actor
    record.reviewed_at = timezone.now()
    record.save(update_fields=["reviewed_by", "reviewed_at", "updated_at"])
    return record


def record_service_period(relationship, actor, *, start_date, end_date, hours, zero_reason="", notes=""):
    """Service hours since the last confirmed entry (one month at a time). The period rolls into the
    month's internal record (superseding it with the new total) and is kept for the trail."""
    from apps.supervision.monthly import parse_decimal

    value = parse_decimal(hours, "Service hours")
    if end_date < start_date:
        raise ValidationError("The period's end can't be before its start.")
    if (start_date.year, start_date.month) != (end_date.year, end_date.month):
        raise ValidationError("A period must stay within one month; split it at the month end.")
    if end_date > timezone.localdate():
        raise ValidationError("A period can't end in the future.")
    if value == 0 and not zero_reason:
        raise ValidationError("Confirming no services for the period needs a reason.")
    overlap = ServiceHoursPeriodEntry.objects.filter(
        supervisee_id=relationship.supervisee_id, record_status="active", start_date__lte=end_date, end_date__gte=start_date,
    ).first()
    if overlap is not None:
        raise ValidationError(f"Hours for {overlap.start_date:%b %d}–{overlap.end_date:%b %d} are already entered; "
                              "periods can't overlap.")
    with transaction.atomic():
        current = ServiceHoursRecord.objects.filter(
            supervisee_id=relationship.supervisee_id, year=start_date.year, month=start_date.month, scope="internal", record_status="active",
        ).first()
        total = (current.hours if current else 0) + value
        record = record_service_hours(
            relationship, actor, year=start_date.year, month=start_date.month, hours=str(total),
            zero_reason=(zero_reason or "no_services_scheduled") if total == 0 else "",
            notes=f"Includes {start_date:%b %d}–{end_date:%b %d}: {value} h" + (f" ({notes})" if notes else ""),
        )
        # Earlier periods of the month now belong to the new record
        ServiceHoursPeriodEntry.objects.filter(record__supervisee_id=relationship.supervisee_id, record__year=start_date.year,
                                               record__month=start_date.month, record_status="active").update(record=record)
        entry = ServiceHoursPeriodEntry.objects.create(
            organization=relationship.organization, supervisee=relationship.supervisee, start_date=start_date, end_date=end_date,
            hours=value, zero_reason=zero_reason or "", notes=notes or "", record=record, entered_by=actor,
        )
    return entry, record


def last_confirmed_through(supervisee_id):
    """The last day the supervisee's service hours are accounted for (periods, or a whole attested month)."""
    from calendar import monthrange

    latest = ServiceHoursPeriodEntry.objects.filter(supervisee_id=supervisee_id, record_status="active").order_by("-end_date").first()
    through = latest.end_date if latest else None
    for record in ServiceHoursRecord.objects.filter(supervisee_id=supervisee_id, scope="internal", record_status="active"):
        if not record.period_entries.filter(record_status="active").exists():
            month_end = date(record.year, record.month, monthrange(record.year, record.month)[1])
            covered = min(month_end, timezone.localtime(record.created_at).date())
            through = covered if through is None or covered > through else through
    return through


def serialize_period(entry):
    return {
        "id": str(entry.id), "start_date": entry.start_date.isoformat(), "end_date": entry.end_date.isoformat(),
        "hours": float(entry.hours), "zero_reason": entry.zero_reason, "notes": entry.notes,
        "entered_by": entry.entered_by.full_name, "entered_at": entry.created_at.isoformat(),
    }


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
        "scope": record.scope,
        "external_organization": record.external_organization,
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
        "reviews": [
            {"reviewer": name(r.reviewer), "relationship_id": str(r.relationship_id) if r.relationship_id else None, "at": r.created_at.isoformat()}
            for r in record.reviews.filter(record_status="active").select_related("reviewer")
        ],
        "periods": [serialize_period(p) for p in record.period_entries.filter(record_status="active").select_related("entered_by")],
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
        records = ServiceHoursRecord.objects.filter(supervisee_id=rel.supervisee_id, year=cycle.year, month=cycle.month,
                                                    record_status="active")
        internal = next((r for r in records if r.scope == "internal"), None)
        inputs["service_hours_record_id"] = str(internal.id) if internal else None
        inputs["external_service_hours_record_ids"] = [str(r.id) for r in records if r.scope == "external"]
        inputs["external_supervision_ids"] = [str(pk) for pk in ExternalSupervisionRecord.objects.filter(
            supervisee_id=rel.supervisee_id, year=cycle.year, month=cycle.month, record_status="active").values_list("id", flat=True)]
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
        # Organization setting: other-agency reporting (or an explicit "none") is required before closing
        if getattr(cycle.organization, "require_external_reporting", False) and rel.supervision_track in SESSION_DERIVED_HOURS_TRACKS:
            reported = ServiceHoursRecord.objects.filter(
                supervisee_id=rel.supervisee_id, year=cycle.year, month=cycle.month, record_status="active",
                scope__in=["external", "external_none"],
            ).exists() or ExternalSupervisionRecord.objects.filter(
                supervisee_id=rel.supervisee_id, year=cycle.year, month=cycle.month, record_status="active").exists()
            if not reported:
                raise ValidationError(
                    "This organization requires other-agency reporting before a month is closed. The supervisee must report "
                    "other-agency hours/supervision or confirm 'no other agency' for this month."
                )
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


# ---------------------------------------------------------------------------
# External supervision (another agency)
# ---------------------------------------------------------------------------

def record_external_supervision(supervisee, actor, *, occurred_on, duration_minutes, supervisor_name, organization_name,
                                session_type="individual", client_observation=False, supervisor_certification_number="",
                                notes="", supporting_reference=""):
    if _closed_months_for_supervisee(supervisee.id, occurred_on.year, occurred_on.month).exists():
        raise ValidationError(f"{occurred_on:%B %Y} is verified and closed. Reopen the month to add a correction.")
    return ExternalSupervisionRecord.objects.create(
        organization=supervisee.organization, supervisee=supervisee, year=occurred_on.year, month=occurred_on.month,
        occurred_on=occurred_on, duration_minutes=duration_minutes, session_type=session_type,
        client_observation=bool(client_observation), supervisor_name=supervisor_name.strip(),
        supervisor_certification_number=(supervisor_certification_number or "").strip(),
        organization_name=organization_name.strip(), notes=notes or "", supporting_reference=supporting_reference or "",
        source_type=ServiceHoursSource.SELF_ATTESTED if actor.id == supervisee.id else ServiceHoursSource.ENTERED_FOR_SUPERVISEE,
        entered_by=actor,
    )


def serialize_external_supervision(row):
    return {
        "id": str(row.id),
        "occurred_on": row.occurred_on.isoformat(),
        "session_type": row.session_type,
        "duration_minutes": row.duration_minutes,
        "client_observation": row.client_observation,
        "supervisor_name": row.supervisor_name,
        "supervisor_certification_number": row.supervisor_certification_number,
        "organization_name": row.organization_name,
        "notes": row.notes,
        "source_type": row.source_type,
        "supporting_reference": row.supporting_reference,
        "entered_by": {"id": str(row.entered_by_id), "name": row.entered_by.full_name},
        "entered_at": row.created_at.isoformat(),
        "reviewed_by": {"id": str(row.reviewed_by_id), "name": row.reviewed_by.full_name} if row.reviewed_by_id else None,
    }
