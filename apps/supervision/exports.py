"""M6 #4: compliance and audit export of one relationship, in one fixed layout, including the
supporting review comments and, for development pathways (BT Development, BCBA Professional
Development), the development records. Read-only."""

import csv
import io
from datetime import date

from apps.core.models import RecordStatus
from apps.supervision.models import (
    COMPLIANCE_BEARING_TRACKS,
    AssessmentStatus,
    CompetencyAssessment,
    ComplianceReminder,
    DevelopmentPlan,
    MonthlyCycle,
    ReviewComment,
    SessionStatus,
    SupervisionSession,
    SupervisionTrack,
)

FORMAT_VERSION = "AKEVRA relationship export v1"


def _month(year, month):
    return date(year, month, 1).strftime("%b %Y")


def _num(value):
    return "" if value is None else value


def _target_label(comment):
    from apps.supervision.models import (
        Assignment,
        DevelopmentPlanMilestone,
        HoursEntry,
        ServiceHoursRecord,
    )

    kind, pk = comment.target_entity_type, comment.target_entity_id
    try:
        if kind == "assignment":
            return f"Assignment: {Assignment.objects.get(pk=pk).title}"
        if kind == "hours_entry":
            e = HoursEntry.objects.get(pk=pk)
            return f"Hours entry: {e.occurred_on:%b %d, %Y}"
        if kind == "service_hours_record":
            r = ServiceHoursRecord.objects.get(pk=pk)
            return f"Service hours: {_month(r.year, r.month)}"
        if kind == "monthly_cycle":
            c = MonthlyCycle.objects.get(pk=pk)
            return f"Monthly cycle: {_month(c.year, c.month)}"
        if kind == "development_plan_milestone":
            return f"Milestone: {DevelopmentPlanMilestone.objects.get(pk=pk).title}"
        if kind == "supervision_session":
            return f"Session: {SupervisionSession.objects.get(pk=pk).occurred_on:%b %d, %Y}"
    except Exception:  # noqa: BLE001 - a missing target still exports the comment
        pass
    return kind.replace("_", " ").capitalize()


def build(relationship):
    """The export as plain data: {meta, months, reminders, comments, development}."""
    from apps.supervision.compliance import relationship_compliance
    from apps.supervision.scheduling import session_type_label
    from apps.supervision.views import _conclusion

    rel = relationship
    cycles = list(MonthlyCycle.objects.filter(relationship=rel).select_related("relationship", "organization").order_by("year", "month"))
    meta = {
        "format": FORMAT_VERSION,
        "organization": rel.organization.name,
        "supervisor": rel.supervisor.full_name,
        "supervisee": rel.supervisee.full_name,
        "supervisee_credential": rel.supervisee.credential_type,
        "pathway": SupervisionTrack(rel.supervision_track).label,
        "compliance_bearing": rel.supervision_track in COMPLIANCE_BEARING_TRACKS,
        "started_on": rel.started_on.isoformat(),
        "status": rel.status,
        "conclusion": _conclusion(rel),
    }
    months = []
    if rel.supervision_track in COMPLIANCE_BEARING_TRACKS:
        data = relationship_compliance(rel, cycles)
        status_by_id = {str(c.id): c.status for c in cycles}
        for m in data.get("months", []):
            checks = m.get("checks") or []
            months.append({
                "month": _month(m["year"], m["month"]),
                "cycle_status": status_by_id.get(m.get("cycle_id"), ""),
                "result": m.get("status", ""),
                "service_or_fieldwork_hours": _num(m.get("service_hours", m.get("total_hours"))),
                "supervision_hours": _num(m.get("supervision_hours")),
                "supervision_percent": _num(m.get("supervision_percent")),
                "required_percent": _num(m.get("required_supervision_percent")),
                "contacts": _num(m.get("contacts")),
                "checks_below": "; ".join(c["label"] for c in checks if c.get("status") == "not_met"),
                "rule_set": (m.get("rules") or {}).get("name", ""),
                "snapshot_closed_at": (m.get("snapshot") or {}).get("closed_at", ""),
            })
    reminders = [
        {"month": _month(r.cycle.year, r.cycle.month), "message": r.message, "status": r.status,
         "resolved_by": r.resolved_by.full_name if r.resolved_by_id else "", "note": r.resolution_note}
        for r in ComplianceReminder.objects.filter(cycle__relationship=rel, record_status="active").select_related("cycle", "resolved_by").order_by("cycle__year", "cycle__month")
    ]
    comments = [
        {"at": c.created_at.isoformat(), "author": c.author.full_name, "on": _target_label(c), "comment": c.body}
        for c in ReviewComment.objects.filter(relationship=rel, record_status="active").select_related("author").order_by("created_at")
    ]
    development = {"sessions": [], "assessments": [], "goals": [], "milestones": []}
    sessions = SupervisionSession.objects.filter(cycle__relationship=rel, record_status=RecordStatus.ACTIVE, status=SessionStatus.COMPLETED).select_related("documented_by").order_by("occurred_on")
    development["sessions"] = [
        {"date": s.occurred_on.isoformat(), "type": session_type_label(s.session_type, rel.supervision_track), "minutes": s.duration_minutes,
         "documented_by": s.documented_by.full_name if s.documented_by_id else "", "topics": s.topics, "feedback": s.feedback}
        for s in sessions
    ]
    development["assessments"] = [
        {"date": a.assessed_on.isoformat(), "competency": a.competency_name, "rating": a.rating,
         "assessed_by": a.assessed_by.full_name if a.assessed_by_id else "", "evidence": a.documented_content}
        for a in CompetencyAssessment.objects.filter(relationship=rel, status=AssessmentStatus.OFFICIAL).select_related("assessed_by").order_by("assessed_on")
    ]
    plan = DevelopmentPlan.objects.filter(relationship=rel, is_current=True).prefetch_related("goals", "milestones").first()
    if plan:
        development["plan_version"] = plan.version_number
        development["goals"] = [{"goal": g.title, "category": g.category, "status": g.status,
                                 "ai_suggestion": str(g.ai_suggestion.suggestion_identifier) if g.ai_suggestion_id else ""}
                                for g in plan.goals.all() if g.record_status == "active"]
        development["milestones"] = [{"milestone": m.title, "status": m.status, "rationale": m.rationale}
                                     for m in plan.milestones.all() if m.record_status == "active"]
    return {"meta": meta, "months": months, "reminders": reminders, "comments": comments, "development": development}


def _section(writer, title, rows):
    writer.writerow([])
    writer.writerow([title])
    if not rows:
        writer.writerow(["(none)"])
        return
    writer.writerow(list(rows[0].keys()))
    for row in rows:
        writer.writerow(["" if v is None else v for v in row.values()])


def to_csv(data):
    out = io.StringIO()
    w = csv.writer(out)
    meta = data["meta"]
    w.writerow([meta["format"]])
    for key in ("organization", "supervisor", "supervisee", "supervisee_credential", "pathway", "started_on", "status"):
        w.writerow([key.replace("_", " ").capitalize(), meta[key]])
    if meta["conclusion"]:
        c = meta["conclusion"]
        w.writerow(["Concluded", f"{c['concluded_on']} — {c['reason_label']}" + (f" ({c['notes']})" if c["notes"] else "")])
    w.writerow(["Compliance-bearing", "Yes" if meta["compliance_bearing"] else "No (development pathway)"])
    if meta["compliance_bearing"]:
        _section(w, "MONTHLY COMPLIANCE", data["months"])
        _section(w, "SHORTFALL REMINDERS", data["reminders"])
    _section(w, "REVIEW COMMENTS", data["comments"])
    dev = data["development"]
    title = "BT DEVELOPMENT RECORDS" if "BT" in meta["pathway"] else "DEVELOPMENT RECORDS"
    _section(w, f"{title} — SESSIONS", dev["sessions"])
    _section(w, f"{title} — COMPETENCY ASSESSMENTS (official)", dev["assessments"])
    _section(w, f"{title} — PLAN GOALS" + (f" (version {dev['plan_version']})" if dev.get("plan_version") else ""), dev["goals"])
    _section(w, f"{title} — MILESTONES", dev["milestones"])
    return out.getvalue()
