"""Audit readiness: the month-by-month supervision log for one supervisee (RBT or BCaBA).

The rows mirror the BACB RBT Supervision Audit Log (hours worked, individual and group
supervision hours, observations with a client, face-to-face contacts, supervisors and
Requirements Coordinators with certification numbers, organization). Totals span every
relationship the supervisee has on the pathway — concluded ones too, since records are kept —
and each month lists the underlying sessions and service-hours record so it can be traced.
Pathway-neutral, so a BCaBA log can be mapped from the same rows.
"""

import csv
import io
from datetime import date
from decimal import Decimal

from django.db.models import Q
from django.utils import timezone

from apps.supervision.models import (
    QUALIFYING_MEETING_METHODS,
    CycleStatus,
    ExternalSupervisionRecord,
    MonthlyCycle,
    RequirementsCoordinatorDesignation,
    ServiceHoursRecord,
    SessionStatus,
    SessionType,
    SupervisionSession,
    SupervisionTrack,
    SupervisoryRelationship,
)

PATHWAYS = {"rbt": SupervisionTrack.RBT_ONGOING, "bcaba": SupervisionTrack.BCABA_ONGOING}
CSV_COLUMNS = [
    "Month", "Year", "Total # of RBT Behavior-Analytic Hours Worked", "Individual Supervision Hours",
    "Group Supervision Hours", "# of Supervisor Observations of RBT with Client", "# of Face to Face Contacts",
    "Supervisor and Requirements Coordinator Name(s) and Certification #(s)",
    "Name of Agency/Organization(s) Where Supervision Occurred",
]


def _months(start_year, start_month, count):
    year, month = start_year, start_month
    for _ in range(count):
        yield year, month
        month += 1
        if month == 13:
            year, month = year + 1, 1


def _month_end(year, month):
    return date(year + (month == 12), month % 12 + 1, 1)


def _person(account, role):
    return {"id": str(account.id), "name": account.full_name, "certification_number": account.certification_number, "role": role}


def _label(person):
    return f"{person['name']} #{person['certification_number']}" if person["certification_number"] else person["name"]


def build(supervisee, *, pathway, start_year, start_month, months=12):
    track = PATHWAYS[pathway]
    relationships = list(SupervisoryRelationship.objects.filter(supervisee=supervisee, supervision_track=track)
                         .select_related("supervisor", "organization"))
    today = timezone.localdate()
    rows = []
    for year, month in _months(start_year, start_month, months):
        first, after = date(year, month, 1), _month_end(year, month)
        in_force = [r for r in relationships if r.started_on < after and (r.ended_on is None or r.ended_on >= first)]
        sessions = list(
            SupervisionSession.objects.filter(
                cycle__relationship__in=relationships, cycle__year=year, cycle__month=month,
                record_status="active", status=SessionStatus.COMPLETED, meeting_method__in=QUALIFYING_MEETING_METHODS,
            ).select_related("cycle__relationship__supervisor", "documented_by").order_by("occurred_on")
        )
        record = ServiceHoursRecord.objects.filter(supervisee=supervisee, year=year, month=month, scope="internal",
                                                   record_status="active").select_related("entered_by", "reviewed_by").first()
        # Another agency (self-reported): part of the RBT's full monthly picture, attributed to its source
        outside_hours = list(ServiceHoursRecord.objects.filter(supervisee=supervisee, year=year, month=month, scope="external", record_status="active"))
        outside = list(ExternalSupervisionRecord.objects.filter(supervisee=supervisee, year=year, month=month, record_status="active").select_related("entered_by"))
        external_service = sum((r.hours for r in outside_hours), Decimal(0))
        minutes = lambda kind: sum(s.duration_minutes for s in sessions if s.session_type == kind) + sum(  # noqa: E731
            e.duration_minutes for e in outside if e.session_type == kind)

        supervisors = {}
        for s in sessions:
            supervisors[s.cycle.relationship.supervisor_id] = _person(s.cycle.relationship.supervisor, "RBT Supervisor")
        if not supervisors:
            for r in in_force:
                supervisors[r.supervisor_id] = _person(r.supervisor, "RBT Supervisor")
        coordinators = [
            _person(d.coordinator, "RBT Requirements Coordinator")
            for d in RequirementsCoordinatorDesignation.objects.filter(
                supervisee=supervisee, record_status="active", starts_on__lt=after
            ).filter(Q(ends_on__isnull=True) | Q(ends_on__gte=first)).select_related("coordinator")
        ]
        external_people = {}
        for e in outside:
            external_people[(e.supervisor_name, e.organization_name)] = {
                "id": None, "name": e.supervisor_name, "certification_number": e.supervisor_certification_number,
                "role": f"External supervisor ({e.organization_name})",
            }
        people = list(supervisors.values()) + list(external_people.values()) + coordinators
        organizations = sorted({r.organization.name for r in (in_force or relationships)}
                               | {e.organization_name for e in outside} | {r.external_organization for r in outside_hours})

        ended = after <= today
        # No relationship on the pathway that month and nothing recorded: outside supervision, not missing
        if not in_force and not sessions and record is None and not outside and not outside_hours:
            rows.append({
                "year": year, "month": month, "month_name": first.strftime("%B"), "period_ended": ended,
                "under_supervision": False, "service_hours": None, "service_hours_status": "not_supervised", "zero_reason": "",
                "individual_supervision_hours": 0.0, "group_supervision_hours": 0.0, "observations_with_client": 0,
                "face_to_face_contacts": 0, "people": [], "people_label": "", "organizations": [], "flags": [],
                "trace": {"service_hours_record": None, "sessions": []},
            })
            continue
        open_cycles = MonthlyCycle.objects.filter(relationship__in=relationships, year=year, month=month).exclude(status=CycleStatus.CLOSED).count()
        flags = []
        if record is None and (ended or sessions):
            flags.append({"code": "missing_service_hours", "message": "Service hours not recorded (missing — not treated as zero)."})
        if record is not None and record.hours > 0 and not sessions and ended:
            flags.append({"code": "no_supervision", "message": "Service hours recorded but no qualifying supervision documented."})
        if record is not None and record.hours == 0 and sessions:
            flags.append({"code": "zero_hours_with_supervision", "message": "Confirmed 0 service hours but supervision was documented."})
        for person in people:
            if not person["certification_number"]:
                flags.append({"code": "missing_certification", "message": f"Certification # missing for {person['name']}."})
        if not supervisee.certification_number:
            flags.append({"code": "missing_supervisee_certification", "message": f"Certification # missing for {supervisee.full_name}."})
        # Two completed sessions for this supervisee on the same date with overlapping times
        for i, first_s in enumerate(sessions):
            for second_s in sessions[i + 1:]:
                if first_s.occurred_on != second_s.occurred_on:
                    continue
                if first_s.start_time and first_s.end_time and second_s.start_time and second_s.end_time:
                    overlap = first_s.start_time < second_s.end_time and second_s.start_time < first_s.end_time
                else:
                    overlap = first_s.duration_minutes == second_s.duration_minutes and first_s.session_type == second_s.session_type
                if overlap:
                    flags.append({"code": "possible_duplicate_session",
                                  "message": f"Possible duplicate sessions on {first_s.occurred_on:%b %d} — review before export."})
        if ended and open_cycles and (record is not None or sessions):
            flags.append({"code": "period_not_closed", "message": "Month has ended but is not verified/closed."})

        rows.append({
            "year": year,
            "month": month,
            "month_name": first.strftime("%B"),
            "period_ended": ended,
            "under_supervision": True,
            "service_hours": None if record is None and not outside_hours else float((record.hours if record else 0) + external_service),
            "internal_service_hours": None if record is None else float(record.hours),
            "external_service_hours": float(external_service),
            "service_hours_status": "missing" if record is None else ("confirmed_zero" if record.hours == 0 else "recorded"),
            "zero_reason": record.get_zero_reason_display() if record is not None and record.zero_reason else "",
            "individual_supervision_hours": float(Decimal(minutes(SessionType.INDIVIDUAL)) / 60),
            "group_supervision_hours": float(Decimal(minutes(SessionType.GROUP)) / 60),
            "observations_with_client": sum(1 for s in sessions if s.client_observation) + sum(1 for e in outside if e.client_observation),
            "face_to_face_contacts": len(sessions) + len(outside),
            "internal_supervision_hours": float(Decimal(sum(s.duration_minutes for s in sessions)) / 60),
            "external_supervision_hours": float(Decimal(sum(e.duration_minutes for e in outside)) / 60),
            "people": people,
            "people_label": "; ".join(_label(p) for p in people),
            "organizations": organizations,
            "flags": flags,
            "trace": {
                "service_hours_record": None if record is None else {
                    "id": str(record.id), "hours": float(record.hours), "entered_by": record.entered_by.full_name,
                    "entered_at": record.created_at.isoformat(), "reviewed_by": ", ".join(r.reviewer.full_name for r in record.reviews.filter(record_status="active").select_related("reviewer")) or None,
                    "source_type": record.source_type,
                },
                "sessions": [
                    {"id": str(s.id), "date": s.occurred_on.isoformat(), "type": s.session_type, "minutes": s.duration_minutes,
                     "meeting_method": s.meeting_method, "client_observation": s.client_observation,
                     "supervisor": s.cycle.relationship.supervisor.full_name, "relationship_id": str(s.cycle.relationship_id)}
                    for s in sessions
                ],
                "external_service_hours": [{"id": str(r.id), "hours": float(r.hours), "organization": r.external_organization} for r in outside_hours],
                "external_supervision": [
                    {"id": str(e.id), "date": e.occurred_on.isoformat(), "type": e.session_type, "minutes": e.duration_minutes,
                     "client_observation": e.client_observation, "supervisor": e.supervisor_name, "organization": e.organization_name,
                     "entered_by": e.entered_by.full_name}
                    for e in outside
                ],
            },
        })
    return {
        "supervisee": _person(supervisee, "RBT" if pathway == "rbt" else "BCaBA"),
        "pathway": pathway,
        "relationship_count": len(relationships),
        "rows": rows,
        "summary": {
            "months": len(rows),
            "missing_service_hours": sum(1 for r in rows if any(f["code"] == "missing_service_hours" for f in r["flags"])),
            "months_not_supervised": sum(1 for r in rows if not r["under_supervision"]),
            "months_with_flags": sum(1 for r in rows if r["flags"]),
            "total_service_hours": sum(r["service_hours"] or 0 for r in rows),
            "total_individual_hours": sum(r["individual_supervision_hours"] for r in rows),
            "total_group_hours": sum(r["group_supervision_hours"] for r in rows),
        },
    }


def to_csv(log):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for r in log["rows"]:
        writer.writerow([
            r["month_name"], r["year"], "" if r["service_hours"] is None else f"{r['service_hours']:.2f}",
            f"{r['individual_supervision_hours']:.2f}", f"{r['group_supervision_hours']:.2f}",
            r["observations_with_client"], r["face_to_face_contacts"], r["people_label"], "; ".join(r["organizations"]),
        ])
    return buffer.getvalue()
