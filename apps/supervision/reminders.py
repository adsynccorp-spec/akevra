"""Shortfall reminders (M6 #3). A reminder is a separate fact from the compliance figure it points at:
it is raised from a month's result, and resolving or dismissing it records what a person did about it.
Nothing here writes to any record a compliance figure is calculated from."""

from datetime import date

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.supervision.models import COMPLIANCE_BEARING_TRACKS, ComplianceReminder, MonthlyCycle, ReminderStatus

OPEN_STATUSES = (ReminderStatus.OPEN,)


def _fmt(value):
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def shortfalls(cycle):
    """What the month's result says is missing: {check_key: message}. Read-only."""
    from apps.supervision.compliance import cycle_compliance

    result = cycle_compliance(cycle)
    month = date(cycle.year, cycle.month, 1).strftime("%b %Y")
    found = {}
    if result.get("status") == "awaiting_service_hours":
        found["service_hours"] = f"{month}: monthly service hours haven't been attested yet."
    results = [result] + list((result.get("by_standard") or {}).values())
    for res in results:
        for check in res.get("checks") or []:
            if check.get("status") == "not_met" or (check.get("met") is False and check.get("status") not in ("not_yet_due", "in_progress")):
                key = check["key"]
                unit = "%" if "percent" in key else ""
                found.setdefault(key, f"{month}: {check['label']} below requirement "
                                      f"({_fmt(check.get('actual'))}{unit} of {_fmt(check.get('required'))}{unit}).")
    return found


def refresh(cycle):
    """Raise a reminder for each new shortfall; clear open reminders whose shortfall is gone. A reminder
    a person resolved or dismissed is not raised again for the same month and check."""
    if cycle.relationship.supervision_track not in COMPLIANCE_BEARING_TRACKS:
        return
    current = shortfalls(cycle)
    existing = {r.check_key: r for r in ComplianceReminder.objects.filter(cycle=cycle, record_status="active")}
    now = timezone.now()
    with transaction.atomic():
        for key, message in current.items():
            if key not in existing:
                ComplianceReminder.objects.create(organization=cycle.organization, cycle=cycle, check_key=key, message=message)
            elif existing[key].status == ReminderStatus.CLEARED:
                # The shortfall came back: reopen it
                existing[key].status = ReminderStatus.OPEN
                existing[key].message = message
                existing[key].resolved_at = None
                existing[key].save(update_fields=["status", "message", "resolved_at", "updated_at"])
        for key, reminder in existing.items():
            if key not in current and reminder.status == ReminderStatus.OPEN:
                reminder.status = ReminderStatus.CLEARED
                reminder.resolved_at = now
                reminder.save(update_fields=["status", "resolved_at", "updated_at"])


def refresh_relationship(relationship):
    for cycle in MonthlyCycle.objects.filter(relationship=relationship).select_related("relationship", "organization"):
        refresh(cycle)


def act(reminder, actor, action, note=""):
    """`resolve` (the follow-up is done) or `dismiss` (not applicable, with a reason)."""
    if reminder.status != ReminderStatus.OPEN:
        raise ValidationError(f"This reminder is already {ReminderStatus(reminder.status).label.lower()}.")
    note = (note or "").strip()
    if action == "dismiss" and not note:
        raise ValidationError("Give a reason for dismissing the reminder.")
    if action not in ("resolve", "dismiss"):
        raise ValidationError("Unknown action.")
    reminder.status = ReminderStatus.RESOLVED if action == "resolve" else ReminderStatus.DISMISSED
    reminder.resolved_by = actor
    reminder.resolved_at = timezone.now()
    reminder.resolution_note = note
    reminder.save(update_fields=["status", "resolved_by", "resolved_at", "resolution_note", "updated_at"])
    return reminder


def serialize(reminder):
    cycle = reminder.cycle
    rel = cycle.relationship
    return {
        "id": str(reminder.id),
        "relationship_id": str(rel.id),
        "supervisee": rel.supervisee.full_name,
        "supervisor": rel.supervisor.full_name,
        "supervision_track": rel.supervision_track,
        "cycle_id": str(cycle.id),
        "year": cycle.year,
        "month": cycle.month,
        "check_key": reminder.check_key,
        "message": reminder.message,
        "status": reminder.status,
        "status_label": ReminderStatus(reminder.status).label,
        "raised_at": reminder.created_at.isoformat(),
        "resolved_at": reminder.resolved_at.isoformat() if reminder.resolved_at else None,
        "resolved_by": reminder.resolved_by.full_name if reminder.resolved_by_id else None,
        "resolution_note": reminder.resolution_note,
    }
