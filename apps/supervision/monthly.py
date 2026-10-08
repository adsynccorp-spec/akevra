"""Monthly Work & Hours: cycles, the assignment lifecycle, and the two hours models."""

import os
import uuid
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.supervision.models import (
    Assignment,
    AssignmentAttachment,
    AssignmentEvent,
    AssignmentStatus,
    FieldworkSubtype,
    HoursEntry,
    HoursStatus,
    SELF_LOGGED_HOURS_TRACKS,
    SESSION_DERIVED_HOURS_TRACKS,
    MonthlyCycle,
    ServiceHoursAttestation,
    SupervisionSession,
    SupervisionTrack,
)

ALLOWED_ATTACHMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx", ".txt", ".png", ".jpg", ".jpeg",
}
MAX_ATTACHMENTS_PER_SUBMISSION = 5

# Which state each assignment state may move to
ASSIGNMENT_TRANSITIONS = {
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
        from_status="" if to_status == AssignmentStatus.ISSUED else assignment.status,
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


def issue_assignment(relationship, actor, *, title, instructions="", due_on=None, year=None, month=None):
    if not (title or "").strip():
        raise ValidationError("An assignment title is required.")
    if year and month:
        cycle = get_or_create_cycle(relationship, int(year), int(month))
    else:
        cycle = cycle_for_date(relationship, due_on or timezone.localdate())
    with transaction.atomic():
        assignment = Assignment.objects.create(
            organization=relationship.organization,
            cycle=cycle,
            title=title.strip(),
            instructions=instructions or "",
            due_on=due_on,
            issued_by=actor,
            status=AssignmentStatus.ISSUED,
        )
        _record(assignment, actor, AssignmentStatus.ISSUED, instructions)
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

HOURS_FIELDS = ("occurred_on", "hours", "kind", "supervision_format", "client_observation", "notes")


def _require_track(relationship, tracks, message):
    # Checked before a cycle is created, so a rejected request leaves nothing behind
    if relationship.supervision_track not in tracks:
        raise ValidationError(message)


def log_fieldwork_hours(relationship, actor, *, occurred_on, hours, kind, supervision_format="",
                        client_observation=False, notes=""):
    _require_track(
        relationship,
        SELF_LOGGED_HOURS_TRACKS,
        "Self-logged hours apply only to Supervised Fieldwork. RBT ongoing-supervision hours "
        "are derived from Supervisor-authored session records.",
    )
    if not occurred_on:
        raise ValidationError("The date worked is required.")
    entry = HoursEntry(
        organization=relationship.organization,
        cycle=cycle_for_date(relationship, occurred_on),
        occurred_on=occurred_on,
        attested_on=timezone.localdate(),
        hours=parse_decimal(hours, "Hours"),
        kind=kind,
        supervision_format=supervision_format or "",
        client_observation=bool(client_observation),
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
    for field, value in changes.items():
        if field not in HOURS_FIELDS or value is None:
            continue
        setattr(entry, field, parse_decimal(value, "Hours") if field == "hours" else value)
    if entry.occurred_on and (entry.occurred_on.year, entry.occurred_on.month) != (entry.cycle.year, entry.cycle.month):
        entry.cycle = cycle_for_date(entry.cycle.relationship, entry.occurred_on)
    entry.status = HoursStatus.PENDING
    entry.return_reason = ""
    entry.attested_on = timezone.localdate()
    entry.save()
    return entry


def verify_fieldwork_hours(entry, actor):
    if entry.status != HoursStatus.PENDING:
        raise ValidationError("Only hours waiting for review can be verified.")
    entry.status = HoursStatus.VERIFIED
    entry.verified_by = actor
    entry.verified_at = timezone.now()
    entry.save()
    return entry


def return_fieldwork_hours(entry, actor, *, reason):
    if entry.status != HoursStatus.PENDING:
        raise ValidationError("Only hours waiting for review can be returned.")
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

def document_session(relationship, actor, *, occurred_on, duration_minutes, session_type,
                     client_observation=False, notes=""):
    if not occurred_on:
        raise ValidationError("The session date is required.")
    try:
        minutes = int(duration_minutes)
    except (TypeError, ValueError):
        raise ValidationError("Duration must be a whole number of minutes.") from None
    session = SupervisionSession(
        organization=relationship.organization,
        cycle=cycle_for_date(relationship, occurred_on),
        occurred_on=occurred_on,
        duration_minutes=minutes,
        session_type=session_type,
        client_observation=bool(client_observation),
        notes=notes or "",
        documented_by=actor,
    )
    session.save()
    return session


def attest_service_hours(relationship, actor, *, year, month, hours, notes=""):
    """One attestation per month; attesting again replaces the figure (the change is audited)."""
    _require_track(
        relationship,
        SESSION_DERIVED_HOURS_TRACKS,
        "Monthly service hours are attested only on RBT or BCaBA ongoing supervision. "
        "Supervised Fieldwork hours are logged and verified instead.",
    )
    cycle = get_or_create_cycle(relationship, int(year), int(month))
    attestation = cycle.service_attestations.filter(record_status="active").first()
    if attestation is None:
        attestation = ServiceHoursAttestation(organization=relationship.organization, cycle=cycle)
    attestation.hours = parse_decimal(hours, "Service hours")
    attestation.attested_by = actor
    attestation.attested_on = timezone.localdate()
    attestation.notes = notes or ""
    attestation.save()
    return attestation


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
        "supervision_format": entry.supervision_format,
        "client_observation": entry.client_observation,
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
        "occurred_on": session.occurred_on.isoformat(),
        "duration_minutes": session.duration_minutes,
        "session_type": session.session_type,
        "client_observation": session.client_observation,
        "notes": session.notes,
        "documented_by": _person(session.documented_by),
    }


def serialize_attestation(attestation):
    if attestation is None:
        return None
    return {
        "id": str(attestation.id),
        "hours": float(attestation.hours),
        "attested_by": _person(attestation.attested_by),
        "attested_on": attestation.attested_on.isoformat(),
        "notes": attestation.notes,
    }


def serialize_cycle(cycle, *, compliance=None, detail=False):
    assignments = [a for a in cycle.assignments.all() if a.record_status == "active"]
    hours = [h for h in cycle.hours_entries.all() if h.record_status == "active"]
    sessions = [s for s in cycle.sessions.all() if s.record_status == "active"]
    attestation = next((a for a in cycle.service_attestations.all() if a.record_status == "active"), None)
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
        "service_hours": serialize_attestation(attestation),
        "compliance": compliance,
    }
    if detail:
        data["assignments"] = [serialize_assignment(a) for a in assignments]
        data["hours_entries"] = [serialize_hours(h) for h in hours]
        data["sessions"] = [serialize_session(s) for s in sessions]
    return data
