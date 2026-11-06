"""Read-only aggregates across the caller's relationships: the Compliance and Competencies pages,
the Reports & Audit activity feed, and the caller's own sign-in sessions.

Nothing here writes. Every list starts from the relationships the caller may already open (the
same `_relationships_with` the Assignments / Hours / Sessions pages use) and every figure comes
from the existing calculations (`relationship_compliance`, the assessment rules), so these pages
can never show more, or compute differently, than the relationship screens themselves.
"""

from collections import Counter
from datetime import date

from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import AuthSession
from apps.audit.models import AuditEvent
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission, org_roles_for, relationship_role_for
from apps.supervision.compliance import relationship_compliance
from apps.supervision.models import (
    AssessmentStatus,
    CompetencyAssessment,
    CompetencyLevel,
    DevelopmentPlan,
    MonthlyCycle,
    SupervisoryRelationship,
)
from apps.supervision.monthly_views import _relationship_summary, _relationships_with

TREND_MONTHS = 6

# Lowest to highest; the four levels of the assessment scale (Not Yet Assessed is not a rating)
LEVELS = [
    CompetencyLevel.NEEDS_TRAINING,
    CompetencyLevel.DEVELOPING,
    CompetencyLevel.PERFORMS_WITH_SUPPORT,
    CompetencyLevel.INDEPENDENT,
]
PROFICIENT = {CompetencyLevel.PERFORMS_WITH_SUPPORT, CompetencyLevel.INDEPENDENT}


def _num(value):
    return None if value is None else float(value)


def _summary(rel, me):
    return {**_relationship_summary(rel), "status": rel.status, "your_role": relationship_role_for(me, rel)}


def _month_ended(month, today):
    return (month["year"], month["month"]) < (today.year, today.month)


def _current_month(months, today):
    """The month shown for a relationship: the latest one that has started (else the latest one)."""
    started = [m for m in months if (m["year"], m["month"]) <= (today.year, today.month)]
    return (started or months or [None])[-1]


def _score(month):
    """Supervision achieved as a share of what is required (capped at 100), when both are known."""
    actual, required = _num(month.get("supervision_percent")), _num(month.get("required_supervision_percent"))
    if actual is None or not required:
        return None
    return round(min(100.0, actual / required * 100), 1)


def _classify(month, today):
    """Deterministic status for the overview, from the month's own result:
    met -> compliant; below requirement in a month that has ended -> at risk; below requirement in
    the open month, or service hours still missing -> needs attention."""
    if month is None:
        return "no_data"
    state = month.get("status")
    if state == "rules_pending":
        return "rules_pending"
    if state == "not_compliance_bearing":
        return "not_tracked"
    if state == "awaiting_service_hours":
        return "needs_attention"
    if month.get("met"):
        return "compliant"
    if state == "shortfall":
        return "at_risk" if _month_ended(month, today) or month.get("snapshot") else "needs_attention"
    return "no_data"


def _unmet(month):
    return [c["label"] for c in month.get("checks", []) if not c.get("met") and c.get("status") != "not_applicable"]


class ComplianceOverviewView(APIView):
    """Compliance across every relationship whose compliance the caller may view."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Compliance across my relationships", operation_id="compliance_overview", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        today = date.today()
        rels = [rel for rel in _relationships_with(request, "compliance.view") if rel.status == "active"]
        cycles_by_rel = {}
        for cycle in (
            MonthlyCycle.objects.filter(relationship__in=rels)
            .select_related("relationship", "closed_by")
            .order_by("year", "month")
        ):
            cycles_by_rel.setdefault(cycle.relationship_id, []).append(cycle)

        trend_keys = []
        y, m = today.year, today.month
        for _ in range(TREND_MONTHS):
            trend_keys.insert(0, (y, m))
            y, m = (y, m - 1) if m > 1 else (y - 1, 12)
        trend = {key: {"evaluated": 0, "met": 0} for key in trend_keys}

        rows, attention = [], []
        for rel in rels:
            result = relationship_compliance(rel, cycles_by_rel.get(rel.id, []))
            months = result.get("months") or []
            month = _current_month(months, today)
            state = "not_tracked" if result.get("status") == "not_compliance_bearing" else _classify(month, today)
            for item in months:
                key = (item["year"], item["month"])
                if key in trend and item.get("status") in ("met", "shortfall"):
                    trend[key]["evaluated"] += 1
                    trend[key]["met"] += 1 if item.get("met") else 0
            past_shortfalls = sum(
                1 for item in months
                if item.get("status") == "shortfall" and _month_ended(item, today) and item is not month
            )
            row = {
                "relationship": _summary(rel, me),
                "cycle": None if month is None else {"id": month.get("cycle_id"), "year": month["year"], "month": month["month"]},
                "required_percent": _num(month.get("required_supervision_percent")) if month else None,
                "current_percent": _num(month.get("supervision_percent")) if month else None,
                "remaining_supervision_hours": _num(month.get("remaining_supervision_hours")) if month else None,
                "score": _score(month) if month else None,
                "month_status": month.get("status") if month else None,
                "status": state,
                "unmet_checks": _unmet(month) if month else [],
                "past_shortfalls": past_shortfalls,
                "months_tracked": len(months),
            }
            rows.append(row)
            if state in ("needs_attention", "at_risk"):
                attention.append(row)

        scored = [r["score"] for r in rows if r["score"] is not None]
        counts = {key: sum(1 for r in rows if r["status"] == key) for key in
                  ("compliant", "needs_attention", "at_risk", "rules_pending", "no_data", "not_tracked")}
        # Lowest score first: the relationships that need action soonest
        attention.sort(key=lambda r: (r["status"] != "at_risk", r["score"] if r["score"] is not None else -1))
        return Response({
            "as_of": today.isoformat(),
            "summary": {
                **counts,
                "total": len(rows),
                "average_score": round(sum(scored) / len(scored), 1) if scored else None,
                "scored": len(scored),
            },
            "trend": [
                {"year": y, "month": m, **trend[(y, m)],
                 "percent": round(trend[(y, m)]["met"] / trend[(y, m)]["evaluated"] * 100, 1) if trend[(y, m)]["evaluated"] else None}
                for (y, m) in trend_keys
            ],
            "relationships": rows,
            "needs_attention": [r["relationship"]["id"] for r in attention],
        })


class CompetencyOverviewView(APIView):
    """Competency assessments across every relationship whose assessments the caller may view.
    Drafts and assessments awaiting review are visible only to those who may manage them, exactly
    as on the relationship's Competency Assessments tab."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Competency Assessments"], summary="Competency assessments across my relationships", operation_id="competency_overview", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        rels = {rel.id: rel for rel in _relationships_with(request, "assessment.view") if rel.status == "active"}
        managers = {rel_id for rel_id, rel in rels.items() if has_permission(me, "assessment.manage", relationship=rel)}
        assessments = list(
            CompetencyAssessment.objects.filter(relationship_id__in=rels, record_status="active")
            .select_related("plan_competency", "related_goal", "assessed_by")
            .order_by("assessed_on", "created_at")
        )
        visible = [a for a in assessments if a.relationship_id in managers or a.status == AssessmentStatus.OFFICIAL]

        # Latest and previous official rating per competency of each relationship
        history = {}
        for a in visible:
            if a.status == AssessmentStatus.OFFICIAL and a.rating in LEVELS:
                key = (a.relationship_id, (a.competency_name or "").strip().lower())
                history.setdefault(key, []).append(a)
        latest = {key: items[-1] for key, items in history.items()}
        regressions = [
            {"assessment": items[-1], "from": items[-2].rating}
            for items in history.values()
            if len(items) > 1 and LEVELS.index(items[-1].rating) < LEVELS.index(items[-2].rating)
        ]

        # Competencies in each current plan that have no official rating yet
        due = []
        for plan in DevelopmentPlan.objects.filter(relationship_id__in=rels, is_current=True).prefetch_related("competencies"):
            for competency in plan.competencies.all():
                if getattr(competency, "record_status", "active") != "active":
                    continue
                if (plan.relationship_id, competency.name.strip().lower()) not in latest:
                    due.append({"relationship_id": str(plan.relationship_id), "competency": competency.name})

        distribution = {level: 0 for level in LEVELS}
        for a in latest.values():
            distribution[a.rating] += 1
        rated = sum(distribution.values())
        proficient = sum(distribution[level] for level in PROFICIENT)

        def row(a):
            rel = rels[a.relationship_id]
            return {
                "id": str(a.id),
                "relationship": _summary(rel, me),
                "competency": a.competency_name or (a.plan_competency.name if a.plan_competency_id else ""),
                "domain": a.plan_competency.domain if a.plan_competency_id else "",
                "related_goal": a.related_goal.title if a.related_goal_id else None,
                "assessed_on": a.assessed_on.isoformat(),
                "rating": a.rating,
                "status": a.status,
                "assessed_by": a.assessed_by.full_name if a.assessed_by_id else None,
                "can_manage": a.relationship_id in managers,
            }

        attention = []
        for a in visible:
            if a.status == AssessmentStatus.READY_FOR_REVIEW and a.relationship_id in managers:
                attention.append({"kind": "pending_review", **row(a)})
        for item in regressions:
            attention.append({"kind": "regression", "previous_rating": item["from"], **row(item["assessment"])})
        for a in latest.values():
            if a.rating == CompetencyLevel.NEEDS_TRAINING:
                attention.append({"kind": "needs_training", **row(a)})

        visible.sort(key=lambda a: (a.assessed_on, a.created_at), reverse=True)
        return Response({
            "summary": {
                "due": len(due),
                "awaiting_review": sum(1 for a in visible if a.status == AssessmentStatus.READY_FOR_REVIEW),
                "in_progress": sum(1 for a in visible if a.status == AssessmentStatus.DRAFT),
                "competent": proficient,
                "needs_attention": distribution[CompetencyLevel.NEEDS_TRAINING] + len(regressions),
                "regressions": len(regressions),
                "rated": rated,
                "mastery_percent": round(proficient / rated * 100, 1) if rated else None,
                "relationships": len(rels),
            },
            "distribution": [
                {"rating": level, "label": CompetencyLevel(level).label, "level": LEVELS.index(level) + 1, "count": distribution[level]}
                for level in reversed(LEVELS)
            ],
            "due": due,
            "attention": attention,
            "assessments": [row(a) for a in visible],
        })


def _actor(event):
    if event.actor_user_account_id:
        return event.actor_user_account.full_name
    return "System"


class AuditActivityView(APIView):
    """Recent audit events. Administrators and Clinical Directors (who hold organization-wide audit
    access) see the organization's events; everyone else sees only actions they took themselves."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Recent audit activity visible to me", operation_id="audit_activity", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        org_wide = has_permission(me, "audit.view")  # without a relationship: Administrator / Clinical Director only
        events = AuditEvent.objects.filter(organization=request.organization).select_related("actor_user_account")
        if not org_wide:
            events = events.filter(actor_user_account=me)
        events = events.exclude(action__endswith=".denied").order_by("-created_at")[:25]
        return Response({
            "scope": "organization" if org_wide else "own",
            "events": [
                {
                    "id": str(e.id),
                    "action": e.action,
                    "entity_type": e.entity_type,
                    "created_at": e.created_at.isoformat(),
                    "actor": _actor(e),
                    "is_me": e.actor_user_account_id == me.id,
                }
                for e in events
            ],
        })


class MySessionsView(APIView):
    """The caller's own signed-in sessions in this workspace (no device details are stored)."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Auth"], summary="My active sign-in sessions", operation_id="my_sessions", responses=JSON_RESPONSE)
    def get(self, request):
        now = timezone.now()
        current = getattr(request, "auth_session", None)
        sessions = AuthSession.objects.filter(
            user_account=request.user_account, revoked_at__isnull=True, absolute_expires_at__gt=now
        ).order_by("-last_seen_at")[:20]
        return Response({
            "sessions": [
                {
                    "id": str(s.id),
                    "started_at": s.created_at.isoformat(),
                    "last_seen_at": s.last_seen_at.isoformat(),
                    "expires_at": s.absolute_expires_at.isoformat(),
                    "current": current is not None and s.id == current.id,
                }
                for s in sessions
            ],
        })


class MyProfileView(APIView):
    """The caller's own profile for Settings: identity, credential, certification number and caseload counts."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Auth"], summary="My profile and caseload", operation_id="my_profile", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        supervising = SupervisoryRelationship.objects.filter(supervisor=me, status="active")
        by_track = Counter(supervising.values_list("supervision_track", flat=True))
        return Response({
            "id": str(me.id),
            "full_name": me.full_name,
            "email": me.email,
            "credential_type": me.credential_type,
            "certification_number": me.certification_number,
            "organization": {"id": str(me.organization_id), "name": me.organization.name},
            "org_roles": sorted(org_roles_for(me)),
            "mfa_enabled": request.user.mfa_enabled,
            "member_since": me.created_at.date().isoformat(),
            "caseload": {"active": sum(by_track.values()), "by_track": dict(by_track)},
            "supervised_by": SupervisoryRelationship.objects.filter(supervisee=me, status="active").count(),
        })
