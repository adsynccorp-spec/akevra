"""In-app notifications (the bell), optionally emailed.

Notifying never blocks or fails the action that caused it: a failed email is logged and the
in-app notification still stands. Nothing here is compliance-bearing.
"""

import logging
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone

from apps.core.email import send_email
from apps.supervision.models import Notification

logger = logging.getLogger(__name__)


def _link(relationship, tab=None):
    if relationship is None:
        return ""
    return f"/dashboard/relationships/{relationship.id}" + (f"?tab={tab}" if tab else "")


def notify(recipient, kind, title, *, body="", relationship=None, tab=None, link=None, appointment=None, email=False):
    """Create one notification; with `email`, also send it to the recipient's address."""
    if recipient is None:
        return None
    notification = Notification.objects.create(
        organization=recipient.organization,
        recipient=recipient,
        kind=kind,
        title=title[:200],
        body=body,
        link=link if link is not None else _link(relationship, tab),
        relationship=relationship,
        appointment=appointment,
    )
    if email and recipient.email:
        url = f"{settings.FRONTEND_URL.rstrip('/')}{notification.link}" if notification.link else settings.FRONTEND_URL
        try:
            sent, _ = send_email(subject=f"AKEVRA: {title}", message=f"{body}\n\nOpen in AKEVRA: {url}".strip(), to=recipient.email)
        except Exception:  # noqa: BLE001 - email is best effort
            logger.exception("Notification email failed (%s)", kind)
            sent = False
        if sent:
            notification.emailed = True
            notification.save(update_fields=["emailed", "updated_at"])
    return notification


def _zone(organization):
    try:
        return ZoneInfo(getattr(organization, "time_zone", "") or "America/New_York")
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo("America/New_York")


def _when(appointment):
    """The session time in the organization's time zone, with the zone named so it is never read as UTC."""
    local = timezone.localtime(appointment.starts_at, _zone(appointment.organization))
    return local.strftime("%b %d, %Y at %I:%M %p %Z").replace(" 0", " ")


def _linked_prep(appointment):
    rel = appointment.relationship
    return appointment.prep_notes.filter(submitted_by=rel.supervisee, record_status="active").first()


# ---------------------------------------------------------------------------
# Scheduling and preparation
# ---------------------------------------------------------------------------

def appointments_scheduled(appointments, actor):
    if not appointments:
        return
    first = appointments[0]
    rel = first.relationship
    more = f" (+{len(appointments) - 1} more weekly)" if len(appointments) > 1 else ""
    prep = _linked_prep(first)
    if prep is not None:
        # Preparation started earlier was attached to this session: no request to prepare again
        submitted = timezone.localtime(prep.submitted_at or prep.updated_at, _zone(first.organization)).strftime("%b %d").replace(" 0", " ")
        ask = (f"Your supervision preparation (submitted {submitted}) is already linked to this session. "
               "You can review or update it from the Sessions tab before the session.")
    else:
        ask = "Please complete your supervision preparation before the session."
    notify(
        rel.supervisee, "appointment_scheduled", f"Supervision scheduled for {_when(first)}{more}",
        body=f"{actor.full_name} scheduled a supervision session for {_when(first)}{more}. {ask}",
        relationship=rel, tab="sessions", appointment=first, email=True,
    )
    if prep is not None:
        # The supervisor learns the waiting preparation is now attached to the session
        notify(
            rel.supervisor, "preparation_submitted", f"{rel.supervisee.full_name}'s supervision preparation is linked",
            body=f"Preparation submitted earlier is now linked to the session on {_when(first)} and is ready for review.",
            relationship=rel, tab="sessions", appointment=first,
        )


def appointment_changed(appointment, actor, action):
    rel = appointment.relationship
    other = rel.supervisee if actor.id == rel.supervisor_id else rel.supervisor
    titles = {
        "rescheduled": f"Supervision moved to {_when(appointment)}",
        "cancelled": f"Supervision on {_when(appointment)} was cancelled",
        "confirmed": f"Supervision on {_when(appointment)} was confirmed",
        "no_show": f"Supervision on {_when(appointment)} was marked No-Show",
    }
    if action in titles:
        notify(other, f"appointment_{action}", titles[action], body=f"By {actor.full_name}.",
               relationship=rel, tab="sessions", appointment=appointment, email=action in ("rescheduled", "cancelled"))


def preparation_submitted(appointment, actor):
    rel = appointment.relationship
    notify(
        rel.supervisor, "preparation_submitted", f"{actor.full_name} submitted supervision preparation",
        body=f"Preparation for the session on {_when(appointment)} is ready for review.",
        relationship=rel, tab="sessions", appointment=appointment, email=True,
    )


_last_reminder_run = {}


def maybe_send_preparation_reminders(organization_id, every_minutes=2):
    """Runs the reminder check for one organization at most every few minutes, from ordinary
    requests (the bell polls), so reminders go out even where no scheduled job is available."""
    now = timezone.now()
    last = _last_reminder_run.get(organization_id)
    if last and (now - last).total_seconds() < every_minutes * 60:
        return 0
    _last_reminder_run[organization_id] = now
    try:
        return send_preparation_reminders(now=now, organization_id=organization_id)
    except Exception:  # noqa: BLE001 - never break the request that triggered it
        logger.exception("Preparation reminder check failed")
        return 0


def send_preparation_reminders(now=None, window_hours=48, organization_id=None):
    """Remind supervisees whose upcoming session (within `window_hours`) has no preparation yet.
    Sends one reminder per appointment; the Supervisor's view already shows Preparation Pending."""
    from apps.supervision.models import Appointment, AppointmentStatus, PreSupervisionNote

    now = now or timezone.now()
    due = Appointment.objects.select_related("relationship__supervisee", "relationship__supervisor").filter(
        status__in=(AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED),
        record_status="active",
        starts_at__gt=now,
        starts_at__lte=now + timedelta(hours=window_hours),
    )
    if organization_id:
        due = due.filter(organization_id=organization_id)
    sent = 0
    for appointment in due:
        rel = appointment.relationship
        prepared = PreSupervisionNote.objects.filter(
            appointment=appointment, submitted_by=rel.supervisee, record_status="active"
        ).exists()
        reminded = Notification.objects.filter(appointment=appointment, kind="preparation_reminder").exists()
        if prepared or reminded:
            continue
        notify(
            rel.supervisee, "preparation_reminder", f"Reminder: complete your preparation for {_when(appointment)}",
            body="Your supervision preparation is still pending for this session.",
            relationship=rel, tab="sessions", appointment=appointment, email=True,
        )
        sent += 1
    return sent


# ---------------------------------------------------------------------------
# Monthly work
# ---------------------------------------------------------------------------

def assignment_event(assignment, actor, action):
    rel = assignment.cycle.relationship
    to_supervisee = action in ("issued", "revision_requested", "completed")
    recipient = rel.supervisee if to_supervisee else rel.supervisor
    titles = {
        "issued": f"New assignment: {assignment.title}",
        "submitted": f"{actor.full_name} submitted “{assignment.title}”",
        "revision_requested": f"Revision requested on “{assignment.title}”",
        "completed": f"“{assignment.title}” was marked complete",
    }
    if action in titles:
        notify(recipient, f"assignment_{action}", titles[action], relationship=rel, tab="assignments",
               email=action in ("issued", "submitted", "revision_requested"))


def hours_event(entry, actor, action):
    rel = entry.cycle.relationship
    if action == "logged":
        notify(rel.supervisor, "hours_logged", f"{actor.full_name} logged {entry.hours} h to verify",
               relationship=rel, tab="hours")
    elif action in ("verified", "returned"):
        notify(rel.supervisee, f"hours_{action}", f"Your {entry.hours} h on {entry.occurred_on:%b %d} were {action}",
               relationship=rel, tab="hours", email=action == "returned")


def session_documented(session, actor):
    rel = session.cycle.relationship
    notify(rel.supervisee, "session_documented", f"Supervision session on {session.occurred_on:%b %d, %Y} was documented",
           body=f"By {actor.full_name}.", relationship=rel, tab="sessions")


def assessment_finalized(assessment, actor):
    rel = assessment.relationship
    name = assessment.competency_name or (assessment.plan_competency.name if assessment.plan_competency_id else "competency")
    notify(rel.supervisee, "assessment_official", f"Competency assessment is official: {name}",
           relationship=rel, tab="assessments")


def serialize(notification):
    return {
        "id": str(notification.id),
        "kind": notification.kind,
        "title": notification.title,
        "body": notification.body,
        "link": notification.link,
        "read": notification.read_at is not None,
        "created_at": notification.created_at.isoformat(),
    }
