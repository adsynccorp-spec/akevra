from django.core.exceptions import ValidationError
from django.db.models import Q, Sum
from django.utils import timezone
from drf_spectacular.utils import OpenApiParameter, extend_schema
from drf_spectacular.types import OpenApiTypes
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import CredentialType, UserAccount
from apps.audit.services import write_audit
from apps.core.api_serializers import (
    JSON_RESPONSE,
    DevelopmentPlanWriteSerializer,
    IntakeCreateSerializer,
    MilestoneTransitionSerializer,
    RelationshipCreateSerializer,
    RelationshipPatchSerializer,
)
from apps.rbac.permissions import HasWorkspace
from apps.rbac.models import RelationshipRole
from apps.rbac.services import (
    can_create_relationship,
    has_permission,
    org_roles_for,
    relationship_role_for,
)
from apps.supervision.models import (
    Appointment,
    Assignment,
    DevelopmentPlan,
    DevelopmentPlanMilestone,
    HoursEntry,
    SupervisionSession,
    SuperviseeAssignmentPurpose,
    SuperviseeIntake,
    SupervisoryRelationship,
    eligible_tracks,
)
from apps.supervision.plan_library import serialize_library
from apps.supervision.services import (
    IntakeAlreadyExists,
    PlanContentImmutable,
    create_development_plan,
    INTAKE_FIELDS,
    create_intake,
    revise_development_plan,
    serialize_intake,
    serialize_plan,
    transition_milestone,
)


def _validation_detail(exc):
    if getattr(exc, "message_dict", None):
        messages = []
        for value in exc.message_dict.values():
            messages.extend(value if isinstance(value, (list, tuple)) else [value])
        return messages[0] if len(messages) == 1 else messages
    messages = getattr(exc, "messages", None)
    if messages:
        return messages[0] if len(messages) == 1 else list(messages)
    return str(exc)


RELATIONSHIP_ID_PARAM = OpenApiParameter(
    name="id",
    type=OpenApiTypes.UUID,
    location=OpenApiParameter.PATH,
    description=(
        "Relationship UUID from GET /api/v1/relationships (the row's top-level `id`). "
        "Not a person id such as supervisor.id or supervisee.id. No trailing spaces."
    ),
)


def _idp_denied_response(user_account, relationship):
    role = relationship_role_for(user_account, relationship)
    if role == RelationshipRole.SUPERVISEE:
        detail = "Supervisee access to the development plan is view-only."
        code = "idp_view_only"
    else:
        detail = "Not permitted."
        code = "idp_manage_denied"
    return Response({"detail": detail, "code": code}, status=status.HTTP_403_FORBIDDEN)


def serialize_relationship(rel, viewer=None):
    data = {
        "id": str(rel.id),
        "organization_id": str(rel.organization_id),
        "supervisor": {
            "id": str(rel.supervisor_id),
            "name": rel.supervisor.full_name,
            "email": rel.supervisor.email,
            "credential_type": rel.supervisor.credential_type,
        },
        "supervisee": {
            "id": str(rel.supervisee_id),
            "name": rel.supervisee.full_name,
            "email": rel.supervisee.email,
            "credential_type": rel.supervisee.credential_type,
        },
        "supervision_track": rel.supervision_track,
        "fieldwork_subtype": rel.fieldwork_subtype,
        "supervisee_purpose": rel.supervisee_purpose,
        "is_compliance_bearing": rel.is_compliance_bearing,
        "started_on": rel.started_on.isoformat(),
        "status": rel.status,
    }
    if viewer is not None:
        data["your_role"] = relationship_role_for(viewer, rel)
        data["permissions"] = {
            name: has_permission(viewer, name, relationship=rel)
            for name in (
                "relationship.view",
                "relationship.edit",
                "relationship.create",
                "intake.manage",
                "idp.manage",
                "idp.view",
                "session.document",
                "session.view",
                "cycle.view",
                "cycle.manage",
                "compliance.view",
                "assignment.create",
                "assignment.submit",
                "assignment.review",
                "hours.log",
                "hours.verify",
                "service_hours.attest",
                "clinical.view",
            )
        }
    return data


def visible_relationships(user_account):
    """Relationships this user may see: their own (as either party), or the whole
    organization for a Clinical Director. RLS already limits rows to the workspace."""
    from apps.rbac.models import OrgRole

    qs = SupervisoryRelationship.objects.select_related("supervisor", "supervisee")
    if OrgRole.CLINICAL_DIRECTOR in org_roles_for(user_account):
        return qs
    return qs.filter(Q(supervisor=user_account) | Q(supervisee=user_account))


def _get_relationship(pk):
    return (
        SupervisoryRelationship.objects.select_related("supervisor", "supervisee")
        .filter(pk=pk)
        .first()
    )


def _plan_queryset():
    return DevelopmentPlan.objects.select_related(
        "relationship", "relationship__supervisor", "relationship__supervisee"
    ).prefetch_related("goals", "competencies", "milestones")


class RelationshipListView(APIView):
    permission_classes = [HasWorkspace]
    rbac_permission = "relationship.view"

    @extend_schema(tags=["Supervision"], summary="List visible relationships", operation_id="relationships_list", responses=JSON_RESPONSE)
    def get(self, request):
        qs = visible_relationships(request.user_account)
        return Response([serialize_relationship(rel, request.user_account) for rel in qs])

    @extend_schema(tags=["Supervision"], summary="Create a relationship", request=RelationshipCreateSerializer, operation_id="relationships_create", responses=JSON_RESPONSE)
    def post(self, request):
        supervisor_id = request.data.get("supervisor_id")
        supervisee_id = request.data.get("supervisee_id")
        try:
            supervisor = UserAccount.objects.get(pk=supervisor_id)
            supervisee = UserAccount.objects.get(pk=supervisee_id)
        except UserAccount.DoesNotExist:
            return Response({"detail": "Unknown party."}, status=status.HTTP_400_BAD_REQUEST)

        if supervisor.credential_type != CredentialType.BCBA:
            write_audit(
                action="relationship.create.denied",
                entity_type="supervisory_relationship",
                entity_id="new",
                request=request,
                metadata={
                    "reason": "supervisor_not_bcba",
                    "supervisor_id": str(supervisor.id),
                    "supervisor_credential": supervisor.credential_type,
                },
            )
            return Response(
                {
                    "detail": (
                        "Version 2.3 role model: Supervisor must be a BCBA. "
                        "BCaBAs and other credentials cannot be assigned as independent Supervisors."
                    ),
                    "code": "supervisor_must_be_bcba",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        if not can_create_relationship(request.user_account, supervisor_id, supervisee_id):
            write_audit(
                action="relationship.create.denied",
                entity_type="supervisory_relationship",
                entity_id="new",
                request=request,
                metadata={"supervisor_id": supervisor_id, "supervisee_id": supervisee_id},
            )
            return Response(
                {"detail": "Not permitted to create this supervisory relationship."},
                status=status.HTTP_403_FORBIDDEN,
            )

        rel = SupervisoryRelationship(
            organization=request.organization,
            supervisor=supervisor,
            supervisee=supervisee,
            supervision_track=request.data.get("supervision_track"),
            fieldwork_subtype=request.data.get("fieldwork_subtype") or "",
            supervisee_purpose=request.data.get("supervisee_purpose") or "",
            started_on=request.data.get("started_on"),
        )
        try:
            rel.save()
        except ValidationError as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="create",
            entity_type="supervisory_relationship",
            entity_id=rel.id,
            request=request,
            after=serialize_relationship(rel),
        )
        return Response(serialize_relationship(rel, request.user_account), status=status.HTTP_201_CREATED)


class RelationshipDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Supervision"], summary="Get one relationship", operation_id="relationships_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _get_relationship(pk)
        if rel is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "relationship.view", relationship=rel):
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        return Response(serialize_relationship(rel, request.user_account))

    @extend_schema(tags=["Supervision"], summary="Edit a relationship", request=RelationshipPatchSerializer, operation_id="relationships_partial_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        rel = _get_relationship(pk)
        if rel is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "relationship.edit", relationship=rel):
            write_audit(
                action="relationship.edit.denied",
                entity_type="supervisory_relationship",
                entity_id=rel.id,
                request=request,
            )
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        before = serialize_relationship(rel)
        for field in ("supervision_track", "fieldwork_subtype", "supervisee_purpose", "status", "ended_on"):
            if field in request.data:
                setattr(rel, field, request.data[field] or "")
        try:
            rel.save()
        except ValidationError as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="update",
            entity_type="supervisory_relationship",
            entity_id=rel.id,
            request=request,
            before=before,
            after=serialize_relationship(rel),
        )
        return Response(serialize_relationship(rel, request.user_account))

    @extend_schema(tags=["Supervision"], summary="Delete is not allowed", operation_id="relationships_delete", responses=JSON_RESPONSE)
    def delete(self, request, pk):
        return Response(
            {"detail": "Hard delete is not permitted."},
            status=status.HTTP_405_METHOD_NOT_ALLOWED,
        )


class EligiblePartiesView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(
        tags=["Supervision"],
        summary="Eligible Supervisors (BCBA only) and Supervisees (including BCBAs and BCaBAs)",
        operation_id="eligible_parties",
        responses=JSON_RESPONSE,
    )
    def get(self, request):
        people = UserAccount.objects.filter(
            organization=request.organization,
            record_status="active",
            status="active",
        ).order_by("last_name", "first_name")

        def party(account):
            return {
                "id": str(account.id),
                "name": account.full_name,
                "email": account.email,
                "credential_type": account.credential_type,
            }

        supervisors = [party(a) for a in people if a.credential_type == CredentialType.BCBA]
        supervisees = [{**party(a), "eligible_tracks": eligible_tracks(a.credential_type)} for a in people]
        return Response(
            {
                "supervisors": supervisors,
                "supervisees": supervisees,
                "notes": {
                    "version_2_3": (
                        "Supervisor is BCBA only. BCaBAs and BCBAs live in the Supervisee "
                        "population and cannot be assigned as independent Supervisors."
                    ),
                    "dec_058": (
                        "The Supervisee population includes BCBAs generally, not only newly "
                        "certified BCBAs. A BCBA may be assigned as Supervisee for structured "
                        "professional development, consultation, competency development, "
                        "leadership development, or case discussion. This adds no fifth role, "
                        "does not change the BCBA-only Supervisor requirement, and does not by "
                        "itself make the relationship compliance-bearing."
                    ),
                    "supervisee_purposes": list(SuperviseeAssignmentPurpose.values),
                },
            }
        )


class IntakeView(APIView):
    permission_classes = [HasWorkspace]

    def _load(self, request, relationship_id, *, write):
        rel = _get_relationship(relationship_id)
        if rel is None:
            return None, Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        needed = "intake.manage" if write else "relationship.view"
        if not has_permission(request.user_account, needed, relationship=rel):
            return None, Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        return rel, None

    @extend_schema(
        tags=["Supervision Setup"],
        summary="Get the intake for a relationship",
        parameters=[RELATIONSHIP_ID_PARAM],
        operation_id="intake_retrieve",
        responses=JSON_RESPONSE,
    )
    def get(self, request, pk):
        rel, error = self._load(request, pk, write=False)
        if error:
            return error
        intake = SuperviseeIntake.objects.filter(relationship=rel).first()
        if intake is None:
            return Response({"detail": "No intake exists for this relationship."}, status=status.HTTP_404_NOT_FOUND)
        return Response(serialize_intake(intake))

    @extend_schema(
        tags=["Supervision Setup"],
        summary="Create the (only) intake for a relationship",
        parameters=[RELATIONSHIP_ID_PARAM],
        request=IntakeCreateSerializer,
        operation_id="intake_create",
        responses=JSON_RESPONSE,
    )
    def post(self, request, pk):
        rel, error = self._load(request, pk, write=True)
        if error:
            return error
        captured_on = request.data.get("captured_on")
        if not captured_on:
            return Response({"detail": "captured_on is required."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            intake = create_intake(
                organization=request.organization,
                relationship=rel,
                captured_on=captured_on,
                **{key: request.data[key] for key in INTAKE_FIELDS if key in request.data},
            )
        except IntakeAlreadyExists as exc:
            write_audit(
                action="intake.create.denied",
                entity_type="supervisee_intake",
                entity_id="duplicate",
                request=request,
                metadata={"relationship_id": str(rel.id), "reason": "duplicate"},
            )
            return Response(
                {"detail": _validation_detail(exc), "code": "intake_duplicate"},
                status=status.HTTP_409_CONFLICT,
            )
        except ValidationError as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="create",
            entity_type="supervisee_intake",
            entity_id=intake.id,
            request=request,
            after=serialize_intake(intake),
        )
        return Response(serialize_intake(intake), status=status.HTTP_201_CREATED)


class DevelopmentPlanListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Development Plans"], summary="List every IDP version for a relationship", operation_id="development_plans_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _get_relationship(pk)
        if rel is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "idp.view", relationship=rel):
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        plans = _plan_queryset().filter(relationship=rel).order_by("plan_family_id", "version_number")
        return Response(
            {
                "relationship_id": str(rel.id),
                "can_manage": has_permission(request.user_account, "idp.manage", relationship=rel),
                "plans": [serialize_plan(plan) for plan in plans],
            }
        )

    @extend_schema(tags=["Development Plans"], summary="Create the first IDP version", request=DevelopmentPlanWriteSerializer, operation_id="development_plans_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _get_relationship(pk)
        if rel is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "idp.manage", relationship=rel):
            write_audit(
                action="idp.manage.denied",
                entity_type="development_plan",
                entity_id="new",
                request=request,
                metadata={"relationship_id": str(rel.id)},
            )
            return _idp_denied_response(request.user_account, rel)
        try:
            plan = create_development_plan(
                organization=request.organization,
                relationship=rel,
                summary=request.data.get("summary") or "",
                goals=request.data.get("goals") or [],
                competencies=request.data.get("competencies") or [],
                milestones=request.data.get("milestones") or [],
            )
        except ValidationError as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="create",
            entity_type="development_plan",
            entity_id=plan.id,
            request=request,
            after=serialize_plan(plan),
        )
        return Response(serialize_plan(plan), status=status.HTTP_201_CREATED)


class DevelopmentPlanDetailView(APIView):
    permission_classes = [HasWorkspace]

    def _load(self, pk):
        return _plan_queryset().filter(pk=pk).first()

    @extend_schema(tags=["Development Plans"], summary="Retrieve one IDP version in full", operation_id="development_plans_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        plan = self._load(pk)
        if plan is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "idp.view", relationship=plan.relationship):
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        payload = serialize_plan(plan)
        payload["can_manage"] = has_permission(
            request.user_account, "idp.manage", relationship=plan.relationship
        )
        return Response(payload)

    @extend_schema(
        tags=["Development Plans"],
        summary="Edit the current IDP (creates a new version; prior version is preserved)",
        request=DevelopmentPlanWriteSerializer,
        operation_id="development_plans_revise",
        responses=JSON_RESPONSE,
    )
    def patch(self, request, pk):
        plan = self._load(pk)
        if plan is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        if not has_permission(request.user_account, "idp.manage", relationship=plan.relationship):
            write_audit(
                action="idp.manage.denied",
                entity_type="development_plan",
                entity_id=plan.id,
                request=request,
            )
            return _idp_denied_response(request.user_account, plan.relationship)
        prior = serialize_plan(plan)
        try:
            revised = revise_development_plan(
                plan,
                summary=request.data.get("summary", plan.summary),
                goals=request.data.get("goals"),
                competencies=request.data.get("competencies"),
                milestones=request.data.get("milestones"),
            )
        except PlanContentImmutable as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        except ValidationError as exc:
            return Response({"detail": _validation_detail(exc)}, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="idp.revise",
            entity_type="development_plan",
            entity_id=revised.id,
            request=request,
            before=prior,
            after=serialize_plan(revised),
            metadata={
                "plan_family_id": str(revised.plan_family_id),
                "prior_version": plan.version_number,
                "new_version": revised.version_number,
            },
        )
        return Response(serialize_plan(revised))


class MilestoneTransitionView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(
        tags=["Development Plans"],
        summary="Transition a milestone from Active to Achieved or Discontinued",
        request=MilestoneTransitionSerializer,
        operation_id="milestone_transition",
        responses=JSON_RESPONSE,
    )
    def post(self, request, pk):
        milestone = (
            DevelopmentPlanMilestone.objects.select_related(
                "plan", "plan__relationship", "plan__relationship__supervisor", "plan__relationship__supervisee"
            )
            .filter(pk=pk)
            .first()
        )
        if milestone is None:
            return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
        rel = milestone.plan.relationship
        if not has_permission(request.user_account, "idp.manage", relationship=rel):
            write_audit(
                action="idp.manage.denied",
                entity_type="development_plan_milestone",
                entity_id=milestone.id,
                request=request,
            )
            return _idp_denied_response(request.user_account, rel)
        try:
            updated = transition_milestone(
                milestone,
                status=request.data.get("status"),
                rationale=request.data.get("rationale") or "",
            )
        except ValidationError as exc:
            detail = _validation_detail(exc)
            payload = {"detail": detail}
            if "rationale" in str(detail).lower():
                payload["code"] = "rationale_required"
            return Response(payload, status=status.HTTP_400_BAD_REQUEST)
        write_audit(
            action="milestone.transition",
            entity_type="development_plan_milestone",
            entity_id=updated.id,
            request=request,
            after={
                "id": str(updated.id),
                "status": updated.status,
                "rationale": updated.rationale,
            },
        )
        return Response(
            {
                "id": str(updated.id),
                "title": updated.title,
                "status": updated.status,
                "rationale": updated.rationale,
                "plan_id": str(updated.plan_id),
            }
        )


class DashboardSummaryView(APIView):
    """Upcoming sessions and this month's cycle totals, scoped to the caller's relationships."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Supervision"], summary="Dashboard: upcoming sessions and cycle aggregates", operation_id="dashboard_summary", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        relationships = list(visible_relationships(me))
        with_sessions = [r for r in relationships if has_permission(me, "session.view", relationship=r)]
        with_cycles = [r for r in relationships if has_permission(me, "cycle.view", relationship=r)]

        now = timezone.now()
        appointments = (
            Appointment.objects.filter(relationship__in=with_sessions, starts_at__gte=now, status="scheduled")
            .select_related("relationship__supervisor", "relationship__supervisee")
            .order_by("starts_at")[:5]
        )

        def with_whom(rel):
            return rel.supervisee.full_name if rel.supervisor_id == me.id else rel.supervisor.full_name

        this_month = {"cycle__relationship__in": with_cycles, "cycle__year": now.year, "cycle__month": now.month}
        hours = HoursEntry.objects.filter(**this_month)
        assignments = Assignment.objects.filter(**this_month)
        return Response({
            "upcoming_sessions": [
                {
                    "id": str(a.id),
                    "relationship_id": str(a.relationship_id),
                    "with": with_whom(a.relationship),
                    "starts_at": a.starts_at.isoformat(),
                }
                for a in appointments
            ],
            "cycle": {
                "year": now.year,
                "month": now.month,
                "assignments_total": assignments.count(),
                "assignments_reviewed": assignments.exclude(status="issued").count(),
                "hours_logged": float(hours.aggregate(total=Sum("hours"))["total"] or 0),
                "hours_verified": float(hours.filter(verified=True).aggregate(total=Sum("hours"))["total"] or 0),
                "sessions_logged": SupervisionSession.objects.filter(**this_month).count(),
            },
        })


class CompetencyListView(APIView):
    """Competencies from the current development plan of every relationship the caller may view."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Development Plans"], summary="Competencies across my relationships (current plan versions)", operation_id="competencies_list", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        relationships = [r for r in visible_relationships(me) if has_permission(me, "idp.view", relationship=r)]
        plans = {
            plan.relationship_id: plan
            for plan in _plan_queryset().filter(relationship__in=relationships, is_current=True)
        }
        results = []
        for rel in relationships:
            plan = plans.get(rel.id)
            results.append({
                "relationship": serialize_relationship(rel),
                "your_role": relationship_role_for(me, rel),
                "can_manage": has_permission(me, "idp.manage", relationship=rel),
                "plan": None if plan is None else {
                    "id": str(plan.id),
                    "version_number": plan.version_number,
                    "competencies": serialize_plan(plan)["competencies"],
                },
            })
        return Response(results)


class PlanLibraryView(APIView):
    """Suggested competencies and milestone templates. Suggestions only; nothing is added to a plan automatically."""

    permission_classes = [HasWorkspace]

    @extend_schema(
        tags=["Development Plans"],
        summary="Suggested competencies (tagged by domain and pathway) and milestone templates",
        parameters=[
            OpenApiParameter(
                name="track",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.QUERY,
                required=False,
                description="Only competencies suggested for this supervision pathway.",
            )
        ],
        operation_id="plan_library",
        responses=JSON_RESPONSE,
    )
    def get(self, request):
        return Response(serialize_library(request.query_params.get("track") or None))
