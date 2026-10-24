"""Scheduling: appointments and pre-supervision preparation notes.

Both are logistics only. Nothing here touches a session record, a contact count, an observation,
an hour or a supervision percentage: those come solely from completed, qualifying session records.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.supervision.models import (
    Appointment,
    AppointmentEvent,
    AppointmentStatus,
    PreSupervisionNote,
    PreSupervisionNoteRevision,
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


def _require_open(appointment, verb, target=None):
    if appointment.status not in OPEN:
        label = AppointmentStatus(appointment.status).label
        if appointment.status == target:
            raise ValidationError(f"This appointment is already marked as {label}.")
        raise ValidationError(f"This appointment is marked as {label}, so it can't be {verb}.")


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
    _require_open(appointment, "cancelled", AppointmentStatus.CANCELLED)
    if not (reason or "").strip():
        raise ValidationError("Give a reason for cancelling.")
    appointment.cancel_reason = reason.strip()
    return _move(appointment, actor, AppointmentStatus.CANCELLED, "cancelled", reason=reason.strip())


def complete(appointment, actor):
    """The meeting happened. This alone counts for nothing: the Supervisor still documents the session."""
    _require_open(appointment, "marked as held", AppointmentStatus.COMPLETED)
    return _move(appointment, actor, AppointmentStatus.COMPLETED, "completed")


def mark_no_show(appointment, actor):
    _require_open(appointment, "marked as a no-show", AppointmentStatus.NO_SHOW)
    return _move(appointment, actor, AppointmentStatus.NO_SHOW, "no_show")


def _move(appointment, actor, status, action, reason=""):
    with transaction.atomic():
        appointment.status = status
        appointment.save()
        _event(appointment, actor, action, reason=reason)
    return appointment


def _fill_prep(note, relationship, *, body="", answers=None):
    from apps.supervision.prep_templates import summary, template_for, validate_answers

    key, template = template_for(relationship.supervision_track)
    if answers is not None:
        note.answers = validate_answers(template, answers)
        note.template_key = key
        note.body = summary(template, note.answers)
    else:
        if not (body or "").strip():
            raise ValidationError("Write your preparation notes before saving.")
        note.body = body.strip()
    note.submitted_at = timezone.now()


def _keep_version(note, actor):
    """Every submission is kept: an edit adds the next version and never replaces the original."""
    last = note.revisions.order_by("-version").first()
    if last is not None and last.answers == (note.answers or {}) and last.body == note.body:
        return last  # nothing changed
    return PreSupervisionNoteRevision.objects.create(
        organization=note.organization, note=note, version=(last.version + 1) if last else 1,
        answers=note.answers or {}, body=note.body, template_key=note.template_key, submitted_by=actor,
        appointment=note.appointment,
    )


def save_prep_note(appointment, actor, body="", answers=None):
    """The Supervisee's preparation (the structured Supervision Preparation Form, or free text);
    one per appointment, editable until the appointment is closed. Each save is kept as a version."""
    if appointment.status not in OPEN:
        raise ValidationError("Preparation notes can only be added to an upcoming appointment.")
    note = PreSupervisionNote.objects.filter(appointment=appointment, submitted_by=actor, record_status="active").first()
    if note is None:
        note = PreSupervisionNote(organization=appointment.organization, appointment=appointment, submitted_by=actor,
                                  relationship=appointment.relationship)
    with transaction.atomic():
        _fill_prep(note, appointment.relationship, body=body, answers=answers)
        note.save()
        _keep_version(note, actor)
    return note


def save_standalone_prep(relationship, actor, answers):
    """Preparation started before an appointment exists; it attaches to the next appointment scheduled."""
    note = PreSupervisionNote.objects.filter(relationship=relationship, appointment__isnull=True, submitted_by=actor,
                                             record_status="active").first()
    if note is None:
        note = PreSupervisionNote(organization=relationship.organization, relationship=relationship, submitted_by=actor)
    with transaction.atomic():
        _fill_prep(note, relationship, answers=answers)
        note.save()
        _keep_version(note, actor)
    return note


def standalone_prep(relationship, actor):
    return PreSupervisionNote.objects.filter(relationship=relationship, appointment__isnull=True, submitted_by=actor,
                                             record_status="active").first()


def linked_preps(relationship, actor):
    """The supervisee's preparations already attached to upcoming sessions, soonest first."""
    return (PreSupervisionNote.objects.filter(relationship=relationship, submitted_by=actor, record_status="active",
                                              appointment__status__in=OPEN, appointment__record_status="active")
            .select_related("appointment", "submitted_by").order_by("appointment__starts_at"))


def attach_waiting_prep(appointment):
    """When a session is scheduled, the supervisee's waiting preparation becomes this appointment's."""
    rel = appointment.relationship
    note = PreSupervisionNote.objects.filter(relationship=rel, appointment__isnull=True, submitted_by=rel.supervisee,
                                             record_status="active").first()
    if note is not None and not appointment.prep_notes.filter(submitted_by=rel.supervisee, record_status="active").exists():
        note.appointment = appointment
        note.save(update_fields=["appointment", "updated_at"])
    return note


def link_session(session, appointment):
    """A documented session may point at the appointment it records; doing so marks the meeting held.
    The appointment still adds nothing: only the session record counts."""
    if appointment.relationship_id != session.cycle.relationship_id:
        raise ValidationError("That appointment belongs to another relationship.")
    if appointment.status in (AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW):
        raise ValidationError("A cancelled or no-show appointment can't be documented.")
    # One event, one session record: documenting the same appointment twice would double-count it
    from apps.supervision.models import SupervisionSession
    already = SupervisionSession.objects.filter(appointment=appointment, record_status="active").exclude(pk=session.pk).first()
    if already is not None:
        raise ValidationError(
            "This appointment is already documented" + (" (a draft is in progress — finish that draft instead)." if already.status == "draft" else ".")
        )
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
    from apps.supervision.prep_templates import TEMPLATES

    return None if note is None else {
        "id": str(note.id),
        "body": note.body,
        "template_key": note.template_key,
        "template_title": TEMPLATES.get(note.template_key, {}).get("title", ""),
        "answers": note.answers or {},
        "submitted_by": _person(note.submitted_by),
        "submitted_at": (note.submitted_at or note.updated_at).isoformat(),
        "appointment_id": str(note.appointment_id) if note.appointment_id else None,
        # The original submission and every later edit, oldest first
        "revisions": [
            {"version": r.version, "answers": r.answers or {}, "body": r.body, "submitted_by": _person(r.submitted_by),
             "submitted_at": r.created_at.isoformat(), "linked": r.appointment_id is not None}
            for r in note.revisions.select_related("submitted_by").order_by("version")
        ],
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
                "previous_ends_at": e.previous_ends_at.isoformat() if e.previous_ends_at else None,
                "starts_at": e.starts_at.isoformat() if e.starts_at else None,
                "ends_at": e.ends_at.isoformat() if e.ends_at else None,
                "reason": e.reason,
            }
            for e in appointment.events.all()
        ]
    return data
