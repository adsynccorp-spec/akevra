"""Scheduling: appointments and pre-supervision preparation notes.

Both are logistics only. Nothing here touches a session record, a contact count, an observation,
an hour or a supervision percentage: those come solely from completed, qualifying session records.
"""

import uuid
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.utils import timezone

from apps.supervision.models import (
    Appointment,
    AppointmentEvent,
    AppointmentStatus,
    AvailabilitySlot,
    PreSupervisionNote,
    PreSupervisionNoteRevision,
    SupervisionTrack,
    Weekday,
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


# ---------------------------------------------------------------------------
# Wording, self-scheduling, conflicts and available times
# ---------------------------------------------------------------------------

# BCBA Professional Development is consultation between BCBAs, not regulatory supervision
SESSION_TYPE_LABELS = {"individual": "Individual Supervision", "group": "Group Supervision"}
CONSULTATION_TYPE_LABELS = {"individual": "Individual Consultation", "group": "Group Consultation"}


def session_type_label(session_type, track):
    labels = CONSULTATION_TYPE_LABELS if track == SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT else SESSION_TYPE_LABELS
    return labels.get(session_type, session_type)


def meeting_word(track):
    return "consultation" if track == SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT else "supervision"


def may_self_schedule(actor, relationship):
    """BCBA Professional Development: the participant books their own consultations with the
    Development Lead. Every other pathway is unchanged (the Supervisor schedules)."""
    return (relationship.supervision_track == SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT
            and actor.id == relationship.supervisee_id and relationship.status == "active")


def _zone(organization):
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        return ZoneInfo(organization.time_zone)
    except (ZoneInfoNotFoundError, ValueError):
        return timezone.get_current_timezone()


def _fmt(moment, organization):
    local = timezone.localtime(moment, _zone(organization))
    return local.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def check_conflicts(relationship, starts_at, ends_at, *, exclude=None):
    """Nobody can be booked twice: an open appointment of either party overlapping this time is refused."""
    people = (relationship.supervisor_id, relationship.supervisee_id)
    clash = (Appointment.objects.filter(record_status="active", status__in=OPEN, starts_at__lt=ends_at, ends_at__gt=starts_at)
             .filter(models.Q(relationship__supervisor_id__in=people) | models.Q(relationship__supervisee_id__in=people))
             .select_related("relationship__supervisor", "relationship__supervisee"))
    if exclude is not None:
        clash = clash.exclude(pk=exclude.pk)
    other = clash.order_by("starts_at").first()
    if other is not None:
        busy = [p for p in (other.relationship.supervisor, other.relationship.supervisee) if p.id in people]
        who = " and ".join(p.full_name for p in busy)
        raise ValidationError(
            f"This time conflicts with another appointment for {who} "
            f"({_fmt(other.starts_at, relationship.organization)}–{_fmt(other.ends_at, relationship.organization).split(', ')[-1]}). "
            "Choose a different time."
        )


def availability_for(owner):
    return list(AvailabilitySlot.objects.filter(owner=owner, record_status="active").order_by("weekday", "starts_at"))


def check_available(relationship, starts_at, ends_at):
    """A self-scheduled consultation must sit inside one of the Development Lead's available times,
    when the Lead has published any."""
    slots = availability_for(relationship.supervisor)
    if not slots:
        return
    zone = _zone(relationship.organization)
    start, end = timezone.localtime(starts_at, zone), timezone.localtime(ends_at, zone)
    fits = start.date() == end.date() and any(
        slot.weekday == start.weekday() and slot.starts_at <= start.time() and end.time() <= slot.ends_at for slot in slots
    )
    if not fits:
        times = "; ".join(f"{Weekday(s.weekday).label} {s.starts_at.strftime('%I:%M %p').lstrip('0')}–{s.ends_at.strftime('%I:%M %p').lstrip('0')}"
                          for s in slots)
        raise ValidationError(f"{relationship.supervisor.full_name} is available at these times only: {times}.")


def replace_availability(owner, actor, slots):
    """Replace the owner's weekly available times. Earlier times are archived, never deleted."""
    cleaned = []
    for slot in slots:
        item = AvailabilitySlot(organization=owner.organization, owner=owner, weekday=slot["weekday"],
                                starts_at=slot["starts_at"], ends_at=slot["ends_at"])
        item.full_clean()
        cleaned.append(item)
    for a in cleaned:
        for b in cleaned:
            if a is not b and a.weekday == b.weekday and a.starts_at < b.ends_at and b.starts_at < a.ends_at:
                raise ValidationError(f"Two available times overlap on {Weekday(a.weekday).label}.")
    with transaction.atomic():
        AvailabilitySlot.objects.filter(owner=owner, record_status="active").update(record_status="archived")
        for item in cleaned:
            item.save()
    return availability_for(owner)


def serialize_slot(slot):
    return {"id": str(slot.id), "weekday": slot.weekday, "weekday_label": Weekday(slot.weekday).label,
            "starts_at": slot.starts_at.strftime("%H:%M"), "ends_at": slot.ends_at.strftime("%H:%M")}


def schedule_appointments(relationship, actor, *, starts_at, ends_at, repeat_weeks=0, **fields):
    """One appointment, or a weekly batch (`repeat_weeks` more after the first) sharing a series id."""
    try:
        repeat_weeks = int(repeat_weeks or 0)
    except (TypeError, ValueError):
        raise ValidationError("Repeat must be a whole number of weeks.") from None
    if not 0 <= repeat_weeks < MAX_BATCH:
        raise ValidationError(f"A batch can hold at most {MAX_BATCH} appointments.")
    if relationship.status != "active":
        raise ValidationError("This relationship is concluded, so no new appointments can be scheduled.")
    series = uuid.uuid4() if repeat_weeks else None
    created = []
    self_scheduled = actor.id == relationship.supervisee_id
    with transaction.atomic():
        for week in range(repeat_weeks + 1):
            if ends_at > starts_at:
                check_conflicts(relationship, starts_at + timedelta(weeks=week), ends_at + timedelta(weeks=week))
                if self_scheduled:
                    check_available(relationship, starts_at + timedelta(weeks=week), ends_at + timedelta(weeks=week))
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
    if moved and appointment.ends_at > appointment.starts_at:
        check_conflicts(appointment.relationship, appointment.starts_at, appointment.ends_at, exclude=appointment)
        if actor.id == appointment.relationship.supervisee_id:
            check_available(appointment.relationship, appointment.starts_at, appointment.ends_at)
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
        "supervision_track": appointment.relationship.supervision_track,
        "starts_at": appointment.starts_at.isoformat(),
        "ends_at": appointment.ends_at.isoformat(),
        "status": appointment.status,
        "session_type": appointment.session_type,
        "session_type_label": session_type_label(appointment.session_type, appointment.relationship.supervision_track),
        "self_scheduled": appointment.created_by_id is not None and appointment.created_by_id == appointment.relationship.supervisee_id,
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
