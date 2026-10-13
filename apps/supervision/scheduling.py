"""Scheduling: appointments and pre-supervision preparation notes.

Both are logistics only. Nothing here touches a session record, a contact count, an observation,
an hour or a supervision percentage: those come solely from completed, qualifying session records.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.supervision.models import (
    Appointment,
    AppointmentEvent,
    AppointmentStatus,
    PreSupervisionNote,
)

MAX_BATCH = 26  # half a year of weekly appointments
OPEN = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)
APPOINTMENT_FIELDS = ("session_type", "meeting_method", "location", "agenda")


def _event(appointment, actor, action, *, previous=None, reason=""):
    return AppointmentEvent.objects.create(
        organization=appointment.organization,
        appointment=appointment,
        action=action,
        actor=actor,
        previous_starts_at=previous[0] if previous else None,
        previous_ends_at=previous[1] if previous else None,
        starts_at=appointment.starts_at,
        ends_at=appointment.ends_at,
        reason=reason or "",
    )


def schedule_appointments(relationship, actor, *, starts_at, ends_at, repeat_weeks=0, **fields):
    """One appointment, or a weekly batch (`repeat_weeks` more after the first) sharing a series id."""
    try:
        repeat_weeks = int(repeat_weeks or 0)
    except (TypeError, ValueError):
        raise ValidationError("Repeat must be a whole number of weeks.") from None
    if not 0 <= repeat_weeks < MAX_BATCH:
        raise ValidationError(f"A batch can hold at most {MAX_BATCH} appointments.")
    series = uuid.uuid4() if repeat_weeks else None
    created = []
    with transaction.atomic():
        for week in range(repeat_weeks + 1):
            appointment = Appointment(
                organization=relationship.organization,
                relationship=relationship,
                starts_at=starts_at + timedelta(weeks=week),
                ends_at=ends_at + timedelta(weeks=week),
                series_id=series,
                created_by=actor,
                status=AppointmentStatus.SCHEDULED,
                **{key: value for key, value in fields.items() if key in APPOINTMENT_FIELDS and value is not None},
            )
            appointment.save()
            _event(appointment, actor, "scheduled")
            created.append(appointment)
    return created


def _require_open(appointment, verb):
    if appointment.status not in OPEN:
        raise ValidationError(f"A {AppointmentStatus(appointment.status).label.lower()} appointment can't be {verb}.")


def reschedule(appointment, actor, *, starts_at=None, ends_at=None, reason="", **fields):
    """New time and/or details. A time change is recorded with the old time and needs re-confirming."""
    _require_open(appointment, "changed")
    previous = (appointment.starts_at, appointment.ends_at)
    moved = (starts_at and starts_at != appointment.starts_at) or (ends_at and ends_at != appointment.ends_at)
    if starts_at:
        appointment.starts_at = starts_at
    if ends_at:
        appointment.ends_at = ends_at
    for key, value in fields.items():
        if key in APPOINTMENT_FIELDS and value is not None:
            setattr(appointment, key, value)
    with transaction.atomic():
        if moved:
            appointment.status = AppointmentStatus.SCHEDULED
        appointment.save()
        _event(appointment, actor, "rescheduled" if moved else "updated", previous=previous if moved else None, reason=reason)
    return appointment


def confirm(appointment, actor):
    if appointment.status != AppointmentStatus.SCHEDULED:
        raise ValidationError("Only a scheduled appointment can be confirmed.")
    return _move(appointment, actor, AppointmentStatus.CONFIRMED, "confirmed")


def cancel(appointment, actor, *, reason):
    _require_open(appointment, "cancelled")
    if not (reason or "").strip():
        raise ValidationError("Give a reason for cancelling.")
    appointment.cancel_reason = reason.strip()
    return _move(appointment, actor, AppointmentStatus.CANCELLED, "cancelled", reason=reason.strip())


def complete(appointment, actor):
    """The meeting happened. This alone counts for nothing: the Supervisor still documents the session."""
    _require_open(appointment, "completed")
    return _move(appointment, actor, AppointmentStatus.COMPLETED, "completed")


def mark_no_show(appointment, actor):
    _require_open(appointment, "marked as a no-show")
    return _move(appointment, actor, AppointmentStatus.NO_SHOW, "no_show")


def _move(appointment, actor, status, action, reason=""):
    with transaction.atomic():
        appointment.status = status
        appointment.save()
        _event(appointment, actor, action, reason=reason)
    return appointment


def save_prep_note(appointment, actor, body):
    """The Supervisee's preparation; one per appointment, editable until the appointment is closed."""
    if not (body or "").strip():
        raise ValidationError("Write your preparation notes before saving.")
    if appointment.status not in OPEN:
        raise ValidationError("Preparation notes can only be added to an upcoming appointment.")
    note = PreSupervisionNote.objects.filter(appointment=appointment, submitted_by=actor, record_status="active").first()
    if note is None:
        note = PreSupervisionNote(organization=appointment.organization, appointment=appointment, submitted_by=actor)
    note.body = body.strip()
    note.save()
    return note


def link_session(session, appointment):
    """A documented session may point at the appointment it records; doing so marks the meeting held.
    The appointment still adds nothing: only the session record counts."""
    if appointment.relationship_id != session.cycle.relationship_id:
        raise ValidationError("That appointment belongs to another relationship.")
    if appointment.status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
        raise ValidationError("A cancelled or no-show appointment can't be documented.")
    session.appointment = appointment
    if appointment.status in OPEN:
        appointment.status = AppointmentStatus.COMPLETED
        appointment.save()
        _event(appointment, session.documented_by, "completed", reason="Session documented")


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _person(account):
    return None if account is None else {"id": str(account.id), "name": account.full_name}


def serialize_prep_note(note):
    return None if note is None else {
        "id": str(note.id),
        "body": note.body,
        "submitted_by": _person(note.submitted_by),
        "submitted_at": note.updated_at.isoformat(),
    }


def serialize_appointment(appointment, *, detail=False):
    notes = [n for n in appointment.prep_notes.all() if n.record_status == "active"]
    sessions = [s for s in appointment.session_records.all() if s.record_status == "active"]
    data = {
        "id": str(appointment.id),
        "relationship_id": str(appointment.relationship_id),
        "starts_at": appointment.starts_at.isoformat(),
        "ends_at": appointment.ends_at.isoformat(),
        "status": appointment.status,
        "session_type": appointment.session_type,
        "meeting_method": appointment.meeting_method,
        "location": appointment.location,
        "agenda": appointment.agenda,
        "series_id": str(appointment.series_id) if appointment.series_id else None,
        "cancel_reason": appointment.cancel_reason,
        "created_by": _person(appointment.created_by),
        "prep_note": serialize_prep_note(notes[0] if notes else None),
        "session": None if not sessions else {"id": str(sessions[0].id), "status": sessions[0].status},
    }
    if detail:
        data["events"] = [
            {
                "action": e.action,
                "actor": _person(e.actor),
                "at": e.created_at.isoformat(),
                "previous_starts_at": e.previous_starts_at.isoformat() if e.previous_starts_at else None,
                "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                "reason": e.reason,
            }
            for e in appointment.events.all()
        ]
    return data
