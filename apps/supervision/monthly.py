"""Monthly Work & Hours: cycles, the assignment lifecycle, and the two hours models."""

import os
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.supervision.compliance import external_hours, service_hours_record
from apps.supervision.periods import ensure_open, record_service_hours, serialize_events, serialize_service_hours
from apps.supervision.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentEvent,
    AssignmentStatus,
    DevelopmentPlanCompetency,
    DevelopmentPlanGoal,
    FieldworkSubtype,
    HoursEntry,
    HoursStatus,
    QUALIFYING_MEETING_METHODS,
    SELF_LOGGED_HOURS_TRACKS,
    SESSION_DERIVED_HOURS_TRACKS,
    MonthlyCycle,
    RelationshipNote,
    SessionAttachment,
    SessionStatus,
    SupervisionSession,
    SupervisionTrack,
)

ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt", ".png", ".jpg", ".jpeg",
}
MAX_ATTACHMENTS_PER_SUBMISSION = 5

# Which state each assignment state may move to
ASSIGNMENT_TRANSITIONS = {
    AssignmentStatus.DRAFT: {AssignmentStatus.ISSUED},
    AssignmentStatus.ISSUED: {AssignmentStatus.SUBMITTED},
    AssignmentStatus.SUBMITTED: {AssignmentStatus.REVISION_REQUESTED, AssignmentStatus.COMPLETED},
    AssignmentStatus.REVISION_REQUESTED: {AssignmentStatus.SUBMITTED},
    AssignmentStatus.COMPLETED: set(),
}


def parse_decimal(value, label):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValidationError(f"{label} must be a number.") from None


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

def _default_fieldwork_type(relationship):
    if relationship.supervision_track != SupervisionTrack.SUPERVISED_FIELDWORK:
        return ""
    return relationship.fieldwork_subtype or FieldworkSubtype.SUPERVISED


def get_or_create_cycle(relationship, year, month, fieldwork_type=None):
    """The relationship's container for that month, created on first use."""
    cycle = MonthlyCycle.objects.filter(relationship=relationship, year=year, month=month).first()
    if cycle:
        return cycle
    try:
        with transaction.atomic():
            return MonthlyCycle.objects.create(
                organization=relationship.organization,
                relationship=relationship,
                year=year,
                month=month,
                fieldwork_type=fieldwork_type or _default_fieldwork_type(relationship),
            )
    except IntegrityError:
        # Another request created it first
        return MonthlyCycle.objects.get(relationship=relationship, year=year, month=month)


def cycle_for_date(relationship, day):
    return get_or_create_cycle(relationship, day.year, day.month)


def create_cycle(relationship, *, year, month, fieldwork_type=""):
    if MonthlyCycle.objects.filter(relationship=relationship, year=year, month=month).exists():
        raise ValidationError(f"A cycle already exists for {year}-{month:02d}.")
    return get_or_create_cycle(relationship, year, month, fieldwork_type or None)


def set_cycle_fieldwork_type(cycle, fieldwork_type):
    cycle.fieldwork_type = fieldwork_type
    cycle.save()
    return cycle


# ---------------------------------------------------------------------------
# Assignments: issue -> submit (with files) -> revision request -> resubmit -> complete
# ---------------------------------------------------------------------------

def _record(assignment, actor, to_status, comment=""):
    """Write the state change; call before assignment.status is updated."""
    return AssignmentEvent.objects.create(
        organization=assignment.organization,
        assignment=assignment,
        from_status=(
            "" if to_status == AssignmentStatus.ISSUED and assignment.status != AssignmentStatus.DRAFT
            else assignment.status
        ),
        to_status=to_status,
        actor=actor,
        comment=comment or "",
    )


def _move(assignment, to_status):
    if to_status not in ASSIGNMENT_TRANSITIONS[assignment.status]:
        raise ValidationError(
            f"An assignment that is {AssignmentStatus(assignment.status).label} can't move to "
            f"{AssignmentStatus(to_status).label}."
        )


ASSIGNMENT_FIELDS = (
    "title", "description", "instructions", "due_on", "assigned_on",
    "related_goal_id", "related_competency_id",
    "requires_written_response", "requires_file", "requires_evidence",
)


def _check_assignment(relationship, assignment):
    if not (assignment.title or "").strip():
        raise ValidationError("An assignment title is required.")
    if assignment.due_on and assignment.assigned_on and assignment.due_on < assignment.assigned_on:
        raise ValidationError("The due date can't be before the assigned date.")
    # Goals and competencies must come from this relationship's development plan
    if assignment.related_goal_id and not DevelopmentPlanGoal.objects.filter(
        pk=assignment.related_goal_id, plan__relationship=relationship
    ).exists():
        raise ValidationError("The related goal isn't part of this relationship's development plan.")
    if assignment.related_competency_id and not DevelopmentPlanCompetency.objects.filter(
        pk=assignment.related_competency_id, plan__relationship=relationship
    ).exists():
        raise ValidationError("The related competency isn't part of this relationship's development plan.")


def _apply(assignment, fields):
    for key, value in fields.items():
        if key in ASSIGNMENT_FIELDS:
            setattr(assignment, key, value.strip() if key == "title" and isinstance(value, str) else value)


def create_assignment(relationship, actor, *, draft=False, year=None, month=None, **fields):
    """Issued straight away, or saved as a draft the Supervisee can't see until it is issued."""
    assignment = Assignment(organization=relationship.organization, issued_by=actor)
    _apply(assignment, fields)
    _check_assignment(relationship, assignment)
    if year and month:
        cycle = get_or_create_cycle(relationship, int(year), int(month))
    else:
        cycle = cycle_for_date(relationship, assignment.due_on or timezone.localdate())
    assignment.cycle = cycle
    assignment.assigned_on = assignment.assigned_on or (None if draft else timezone.localdate())
    with transaction.atomic():
        assignment.status = AssignmentStatus.DRAFT if draft else AssignmentStatus.ISSUED
        assignment.save()
        if not draft:
            _record(assignment, actor, AssignmentStatus.ISSUED, assignment.instructions)
    return assignment


def issue_assignment(relationship, actor, *, title, instructions="", due_on=None, year=None, month=None, **fields):
    return create_assignment(relationship, actor, title=title, instructions=instructions, due_on=due_on,
                             year=year, month=month, **fields)


def update_draft_assignment(assignment, *, year=None, month=None, **fields):
    if assignment.status != AssignmentStatus.DRAFT:
        raise ValidationError("Only a draft assignment can be edited.")
    relationship = assignment.cycle.relationship
    _apply(assignment, fields)
    _check_assignment(relationship, assignment)
    if year and month:
        assignment.cycle = get_or_create_cycle(relationship, int(year), int(month))
    assignment.save()
    return assignment


def issue_draft_assignment(assignment, actor):
    _move(assignment, AssignmentStatus.ISSUED)
    with transaction.atomic():
        _record(assignment, actor, AssignmentStatus.ISSUED, assignment.instructions)
        assignment.status = AssignmentStatus.ISSUED
        assignment.assigned_on = assignment.assigned_on or timezone.localdate()
        assignment.save(update_fields=["status", "assigned_on", "updated_at"])
    return assignment


def validate_upload(upload):
    name = os.path.basename(upload.name or "")
    extension = os.path.splitext(name)[1].lower()
    if extension not in ALLOWED_ATTACHMENT_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_ATTACHMENT_EXTENSIONS))
        raise ValidationError(f"'{name}' isn't an accepted file type. Accepted: {allowed}.")
    limit = settings.ASSIGNMENT_ATTACHMENT_MAX_BYTES
    if upload.size > limit:
        raise ValidationError(f"'{name}' is larger than {limit // (1024 * 1024)} MB.")
    return name


def submit_assignment(assignment, actor, *, note="", files=()):
    files = list(files or [])
    if not files and not (note or "").strip():
        raise ValidationError("A submission needs at least one file or a note.")
    if assignment.requires_written_response and not (note or "").strip():
        raise ValidationError("This assignment requires a written response.")
    if (assignment.requires_file or assignment.requires_evidence) and not files:
        raise ValidationError("This assignment requires an attached file.")
    if len(files) > MAX_ATTACHMENTS_PER_SUBMISSION:
        raise ValidationError(f"Attach no more than {MAX_ATTACHMENTS_PER_SUBMISSION} files per submission.")
    names = [validate_upload(upload) for upload in files]
    _move(assignment, AssignmentStatus.SUBMITTED)
    with transaction.atomic():
        event = _record(assignment, actor, AssignmentStatus.SUBMITTED, note)
        for upload, name in zip(files, names):
            attachment = AssignmentAttachment(
                id=uuid.uuid4(),
                organization=assignment.organization,
                assignment=assignment,
                event=event,
                original_name=name,
                content_type=getattr(upload, "content_type", "") or "",
                size_bytes=upload.size,
                uploaded_by=actor,
            )
            attachment.file.save(name, upload, save=False)
            attachment.save()
        assignment.status = AssignmentStatus.SUBMITTED
        assignment.evidence_notes = note or ""
        assignment.save(update_fields=["status", "evidence_notes", "updated_at"])
    return assignment


def request_revision(assignment, actor, *, comment):
    if not (comment or "").strip():
        raise ValidationError("Say what needs to change when requesting a revision.")
    _move(assignment, AssignmentStatus.REVISION_REQUESTED)
    with transaction.atomic():
        _record(assignment, actor, AssignmentStatus.REVISION_REQUESTED, comment.strip())
        assignment.status = AssignmentStatus.REVISION_REQUESTED
        assignment.save(update_fields=["status", "updated_at"])
    return assignment


def complete_assignment(assignment, actor, *, comment=""):
    _move(assignment, AssignmentStatus.COMPLETED)
    with transaction.atomic():
        _record(assignment, actor, AssignmentStatus.COMPLETED, comment)
        assignment.status = AssignmentStatus.COMPLETED
        assignment.save(update_fields=["status", "updated_at"])
    return assignment


# ---------------------------------------------------------------------------
# Model 1 — Supervised Fieldwork: self-logged by the trainee, verified by the Supervisor
# ---------------------------------------------------------------------------

HOURS_FIELDS = ("occurred_on", "hours", "kind", "activity_category", "supervision_format", "client_observation",
                "observation_minutes", "notes")


def _require_track(relationship, tracks, message):
    # Checked before a cycle is created, so a rejected request leaves nothing behind
    if relationship.supervision_track not in tracks:
        raise ValidationError(message)


def log_fieldwork_hours(relationship, actor, *, occurred_on, hours, kind, supervision_format="",
                        client_observation=False, observation_minutes=None, notes="", activity_category="unrestricted"):
    _require_track(
        relationship,
        SELF_LOGGED_HOURS_TRACKS,
        "Self-logged hours apply only to Supervised Fieldwork. RBT ongoing-supervision hours "
        "are derived from Supervisor-authored session records.",
    )
    if not occurred_on:
        raise ValidationError("The date worked is required.")
    if client_observation and observation_minutes in (None, ""):
        # BACB 2027 counts observation by duration, so new observations record their minutes
        raise ValidationError("Enter how many minutes of this supervision were spent observing you with a client.")
    cycle = cycle_for_date(relationship, occurred_on)
    ensure_open(cycle, "hours")
    entry = HoursEntry(
        organization=relationship.organization,
        cycle=cycle,
        occurred_on=occurred_on,
        attested_on=timezone.localdate(),
        hours=parse_decimal(hours, "Hours"),
        kind=kind,
        activity_category=activity_category or "unrestricted",
        supervision_format=supervision_format or "",
        client_observation=bool(client_observation),
        observation_minutes=observation_minutes if client_observation else None,
        notes=notes or "",
        logged_by=actor,
        status=HoursStatus.PENDING,
    )
    entry.save()
    return entry


def correct_fieldwork_hours(entry, **changes):
    """The Supervisee fixes a pending or returned entry; it goes back for verification."""
    if entry.status == HoursStatus.VERIFIED:
        raise ValidationError("Verified hours can't be changed.")
    ensure_open(entry.cycle, "hours")
    for field, value in changes.items():
        if field not in HOURS_FIELDS or value is None:
            continue
        setattr(entry, field, parse_decimal(value, "Hours") if field == "hours" else value)
    if entry.occurred_on and (entry.occurred_on.year, entry.occurred_on.month) != (entry.cycle.year, entry.cycle.month):
        entry.cycle = cycle_for_date(entry.cycle.relationship, entry.occurred_on)
        ensure_open(entry.cycle, "hours")
    if entry.client_observation and entry.observation_minutes is None:
        raise ValidationError("Enter how many minutes of this supervision were spent observing you with a client.")
    entry.status = HoursStatus.PENDING
    entry.return_reason = ""
    entry.attested_on = timezone.localdate()
    entry.save()
    return entry


def verify_fieldwork_hours(entry, actor):
    if entry.status != HoursStatus.PENDING:
        raise ValidationError("Only hours waiting for review can be verified.")
    ensure_open(entry.cycle, "hours")
    entry.status = HoursStatus.VERIFIED
    entry.verified_by = actor
    entry.verified_at = timezone.now()
    entry.save()
    return entry


def record_observation_minutes(entry, actor, minutes):
    """The Supervisor records how long an observation lasted on an entry logged before durations
    were captured (BACB 2027 counts observation by duration). Only the duration changes: the hours,
    status and verification stay as they are, and the change is audited."""
    if not entry.client_observation:
        raise ValidationError("This entry has no client observation.")
    ensure_open(entry.cycle, "hours")
    try:
        minutes = int(minutes)
    except (TypeError, ValueError):
        raise ValidationError("Enter the observation length in whole minutes.") from None
    if minutes < 1:
        raise ValidationError("Enter the observation length in whole minutes.")
    entry.observation_minutes = minutes
    entry.save()
    return entry


def observation_minutes_history(entry):
    """Every change a Supervisor made to an entry's observation duration, oldest first (from the audit trail)."""
    from apps.audit.models import AuditEvent

    events = (AuditEvent.objects.filter(entity_type="hours_entry", entity_id=str(entry.id), action="hours.observation_minutes")
              .select_related("actor_user_account").order_by("created_at"))
    return [{
        "before": (event.before or {}).get("observation_minutes"),
        "after": (event.after or {}).get("observation_minutes"),
        "by": event.actor_user_account.full_name if event.actor_user_account_id else None,
        "at": event.created_at.isoformat(),
    } for event in events]


def return_fieldwork_hours(entry, actor, *, reason):
    if entry.status != HoursStatus.PENDING:
        raise ValidationError("Only hours waiting for review can be returned.")
    ensure_open(entry.cycle, "hours")
    if not (reason or "").strip():
        raise ValidationError("Say what needs correcting when returning hours.")
    entry.status = HoursStatus.RETURNED
    entry.return_reason = reason.strip()
    entry.verified_by = None
    entry.verified_at = None
    entry.save()
    return entry


# ---------------------------------------------------------------------------
# Model 2 — RBT / BCaBA ongoing: Supervisor-authored sessions + self-attested service hours
# ---------------------------------------------------------------------------

SESSION_FIELDS = (
    "occurred_on", "duration_minutes", "session_type", "client_observation", "notes",
    "start_time", "end_time", "meeting_method", "topics", "activities", "feedback", "follow_up",
)


def _minutes_between(start, end):
    return (end.hour * 60 + end.minute) - (start.hour * 60 + start.minute)


def _save_session_files(session, actor, files):
    files = list(files or [])
    if len(files) > MAX_ATTACHMENTS_PER_SUBMISSION:
        raise ValidationError(f"Attach no more than {MAX_ATTACHMENTS_PER_SUBMISSION} files.")
    names = [validate_upload(upload) for upload in files]
    for upload, name in zip(files, names):
        attachment = SessionAttachment(
            id=uuid.uuid4(),
            organization=session.organization,
            session=session,
            original_name=name,
            content_type=getattr(upload, "content_type", "") or "",
            size_bytes=upload.size,
            uploaded_by=actor,
        )
        attachment.file.save(name, upload, save=False)
        attachment.save()


def _fill_session(session, fields):
    for key, value in fields.items():
        if key in SESSION_FIELDS and value is not None:
            setattr(session, key, value)
    if session.start_time and session.end_time:
        session.duration_minutes = _minutes_between(session.start_time, session.end_time)
    if not session.occurred_on:
        raise ValidationError("The session date is required.")
    try:
        session.duration_minutes = int(session.duration_minutes)
    except (TypeError, ValueError):
        raise ValidationError("Give the session's start and end times, or its duration in minutes.") from None


def _appointment(appointment_id):
    from apps.supervision.models import Appointment

    appointment = Appointment.objects.filter(pk=appointment_id).first()
    if appointment is None:
        raise ValidationError("That appointment doesn't exist.")
    return appointment


def _minutes_of(value):
    return value.hour * 60 + value.minute


def ensure_not_duplicate(session):
    """A formal session can't be documented twice: another completed session for the same
    supervisee on the same date whose time overlaps (or, without times, with the same duration,
    format and method) would give duplicate compliance credit."""
    rel = session.cycle.relationship
    others = SupervisionSession.objects.filter(
        cycle__relationship__supervisee_id=rel.supervisee_id, occurred_on=session.occurred_on,
        status=SessionStatus.COMPLETED, record_status="active",
    ).exclude(pk=session.pk).select_related("cycle__relationship__supervisor")
    for other in others:
        if session.start_time and session.end_time and other.start_time and other.end_time:
            clash = _minutes_of(session.start_time) < _minutes_of(other.end_time) and _minutes_of(other.start_time) < _minutes_of(session.end_time)
        else:
            clash = (other.duration_minutes == session.duration_minutes and other.session_type == session.session_type
                     and (other.meeting_method or "") == (session.meeting_method or ""))
        if clash:
            when = f" {other.start_time:%H:%M}–{other.end_time:%H:%M}" if other.start_time and other.end_time else ""
            raise ValidationError(
                f"A completed session is already documented for {rel.supervisee.full_name} on {other.occurred_on:%b %d, %Y}{when} "
                f"(by {other.cycle.relationship.supervisor.full_name}). Duplicate sessions can't count twice — open the existing record instead."
            )


def document_session(relationship, actor, *, draft=False, files=(), appointment_id=None, **fields):
    """A completed record counts toward requirements; a draft doesn't until it is completed."""
    from apps.supervision.scheduling import link_session

    session = SupervisionSession(organization=relationship.organization, documented_by=actor)
    _fill_session(session, fields)
    session.cycle = cycle_for_date(relationship, session.occurred_on)
    session.status = SessionStatus.DRAFT if draft else SessionStatus.COMPLETED
    if not draft:
        ensure_open(session.cycle, "supervision sessions")
        ensure_not_duplicate(session)
    with transaction.atomic():
        if appointment_id:
            link_session(session, _appointment(appointment_id))
        session.save()
        _save_session_files(session, actor, files)
    return session


def update_session(session, actor, *, complete=False, files=(), appointment_id=None, **fields):
    """Edit a draft, optionally completing it. Completed records are never edited."""
    from apps.supervision.scheduling import link_session

    if session.status != SessionStatus.DRAFT:
        raise ValidationError("A completed session record can't be changed.")
    if appointment_id:
        link_session(session, _appointment(appointment_id))
    _fill_session(session, fields)
    if (session.occurred_on.year, session.occurred_on.month) != (session.cycle.year, session.cycle.month):
        session.cycle = cycle_for_date(session.cycle.relationship, session.occurred_on)
    if complete:
        ensure_open(session.cycle, "supervision sessions")
        ensure_not_duplicate(session)
        session.status = SessionStatus.COMPLETED
    with transaction.atomic():
        session.save()
        _save_session_files(session, actor, files)
    return session


def add_note(relationship, actor, body):
    if not (body or "").strip():
        raise ValidationError("Write something before saving the note.")
    return RelationshipNote.objects.create(
        organization=relationship.organization, relationship=relationship, author=actor, body=body.strip()
    )


def attest_service_hours(relationship, actor, *, year, month, hours, notes="", zero_reason="", supporting_reference="",
                         scope="internal", external_organization=""):
    """The supervisee's monthly service hours by source (see periods.record_service_hours)."""
    return record_service_hours(relationship, actor, year=year, month=month, hours=hours, notes=notes,
                                zero_reason=zero_reason, supporting_reference=supporting_reference,
                                scope=scope, external_organization=external_organization)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _person(account):
    return None if account is None else {"id": str(account.id), "name": account.full_name}


def serialize_attachment(attachment):
    return {
        "id": str(attachment.id),
        "name": attachment.original_name,
        "content_type": attachment.content_type,
        "size_bytes": attachment.size_bytes,
        "uploaded_by": _person(attachment.uploaded_by),
        "uploaded_at": attachment.created_at.isoformat(),
        "download_url": f"/api/v1/assignment-attachments/{attachment.id}/download",
    }


def serialize_assignment(assignment, *, detail=False):
    data = {
        "id": str(assignment.id),
        "cycle_id": str(assignment.cycle_id),
        "year": assignment.cycle.year,
        "month": assignment.cycle.month,
        "title": assignment.title,
        "instructions": assignment.instructions,
        "status": assignment.status,
        "due_on": assignment.due_on.isoformat() if assignment.due_on else None,
        "issued_by": _person(assignment.issued_by),
        "issued_at": assignment.created_at.isoformat(),
        "attachment_count": len(assignment.attachments.all()),
        "description": assignment.description,
        "assigned_on": assignment.assigned_on.isoformat() if assignment.assigned_on else None,
        "related_goal": None if assignment.related_goal_id is None else {
            "id": str(assignment.related_goal_id), "title": assignment.related_goal.title,
        },
        "related_competency": None if assignment.related_competency_id is None else {
            "id": str(assignment.related_competency_id), "name": assignment.related_competency.name,
        },
        "requirements": {
            "written_response": assignment.requires_written_response,
            "file": assignment.requires_file,
            "evidence": assignment.requires_evidence,
        },
    }
    if detail:
        attachments_by_event = {}
        for attachment in assignment.attachments.all():
            attachments_by_event.setdefault(attachment.event_id, []).append(serialize_attachment(attachment))
        data["events"] = [
            {
                "id": str(event.id),
                "from_status": event.from_status,
                "to_status": event.to_status,
                "actor": _person(event.actor),
                "comment": event.comment,
                "at": event.created_at.isoformat(),
                "attachments": attachments_by_event.get(event.id, []),
            }
            for event in assignment.events.all()
        ]
    return data


def serialize_hours(entry):
    return {
        "id": str(entry.id),
        "cycle_id": str(entry.cycle_id),
        "occurred_on": entry.occurred_on.isoformat(),
        "attested_on": entry.attested_on.isoformat(),
        "hours": float(entry.hours),
        "kind": entry.kind,
        "activity_category": entry.activity_category,
        "supervision_format": entry.supervision_format,
        "client_observation": entry.client_observation,
        "observation_minutes": entry.observation_minutes,
        "notes": entry.notes,
        "status": entry.status,
        "return_reason": entry.return_reason,
        "logged_by": _person(entry.logged_by),
        "verified_by": _person(entry.verified_by),
        "verified_at": entry.verified_at.isoformat() if entry.verified_at else None,
    }


def serialize_session(session):
    return {
        "id": str(session.id),
        "cycle_id": str(session.cycle_id),
        "status": session.status,
        "appointment_id": str(session.appointment_id) if session.appointment_id else None,
        # Completed and real-time (in person or live video): the only records that count
        "qualifies": session.status == SessionStatus.COMPLETED and session.meeting_method in QUALIFYING_MEETING_METHODS,
        "occurred_on": session.occurred_on.isoformat(),
        "start_time": session.start_time.strftime("%H:%M") if session.start_time else None,
        "end_time": session.end_time.strftime("%H:%M") if session.end_time else None,
        "duration_minutes": session.duration_minutes,
        "session_type": session.session_type,
        "meeting_method": session.meeting_method,
        "client_observation": session.client_observation,
        "topics": session.topics,
        "activities": session.activities,
        "feedback": session.feedback,
        "follow_up": session.follow_up,
        "notes": session.notes,
        "documented_by": _person(session.documented_by),
        "documented_at": session.updated_at.isoformat(),
        "attachments": [
            {
                "id": str(a.id),
                "name": a.original_name,
                "size_bytes": a.size_bytes,
                "uploaded_at": a.created_at.isoformat(),
                "download_url": f"/api/v1/session-attachments/{a.id}/download",
            }
            for a in session.attachments.all() if a.record_status == "active"
        ],
    }


def serialize_note(note):
    return {
        "id": str(note.id),
        "body": note.body,
        "author": _person(note.author),
        "created_at": note.created_at.isoformat(),
    }


def serialize_attestation(record):
    return serialize_service_hours(record)


def serialize_cycle(cycle, *, compliance=None, detail=False):
    assignments = [a for a in cycle.assignments.all() if a.record_status == "active"]
    hours = [h for h in cycle.hours_entries.all() if h.record_status == "active"]
    # Only completed records count; drafts are the Supervisor's work in progress
    sessions = [s for s in cycle.sessions.all() if s.record_status == "active" and s.status == SessionStatus.COMPLETED]
    record = service_hours_record(cycle.relationship.supervisee_id, cycle.year, cycle.month)
    data = {
        "id": str(cycle.id),
        "relationship_id": str(cycle.relationship_id),
        "year": cycle.year,
        "month": cycle.month,
        "fieldwork_type": cycle.fieldwork_type,
        "status": cycle.status,
        "counts": {
            "assignments": len(assignments),
            "assignments_completed": sum(1 for a in assignments if a.status == AssignmentStatus.COMPLETED),
            "hours_entries": len(hours),
            "hours_logged": float(sum((h.hours for h in hours), Decimal("0"))),
            "hours_verified": float(sum((h.hours for h in hours if h.status == HoursStatus.VERIFIED), Decimal("0"))),
            "sessions": len(sessions),
            "session_minutes": sum(s.duration_minutes for s in sessions),
        },
        "service_hours": serialize_service_hours(record),
        "external_service_hours": [serialize_service_hours(r) for r in external_hours(cycle.relationship.supervisee_id, cycle.year, cycle.month)],
        "closed_at": cycle.closed_at.isoformat() if cycle.closed_at else None,
        "closed_by": cycle.closed_by.full_name if cycle.closed_by_id else None,
        "compliance": compliance,
    }
    if detail:
        data["assignments"] = [serialize_assignment(a) for a in assignments]
        data["hours_entries"] = [serialize_hours(h) for h in hours]
        data["sessions"] = [serialize_session(s) for s in sessions]
        data["status_history"] = serialize_events(cycle)
    return data
