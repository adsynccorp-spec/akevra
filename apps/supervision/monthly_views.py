"""Monthly Work & Hours API: cycles, assignments, fieldwork hours, sessions, service hours, compliance."""

from django.core.exceptions import ValidationError
from django.http import FileResponse
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import (
    JSON_RESPONSE,
    AssignmentCreateSerializer,
    AssignmentReviewSerializer,
    AssignmentSubmitSerializer,
    CycleCreateSerializer,
    CyclePatchSerializer,
    HoursCreateSerializer,
    HoursPatchSerializer,
    HoursReturnSerializer,
    ServiceHoursSerializer,
    SessionCreateSerializer,
)
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission
from apps.supervision import monthly
from apps.supervision.compliance import cycle_compliance, relationship_compliance
from apps.supervision.models import (
    Assignment,
    AssignmentAttachment,
    HoursEntry,
    MonthlyCycle,
    SupervisionSession,
    SupervisoryRelationship,
)


def _detail(exc):
    messages = getattr(exc, "messages", None) or [str(exc)]
    return messages[0] if len(messages) == 1 else list(messages)


def _bad_request(exc):
    return Response({"detail": _detail(exc)}, status=status.HTTP_400_BAD_REQUEST)


def _invalid(serializer):
    # One readable line per field, the shape the frontend already shows ({detail: string | string[]})
    messages = [
        f"{field.replace('_', ' ').capitalize()}: {' '.join(str(e) for e in errors)}"
        for field, errors in serializer.errors.items()
    ]
    return Response({"detail": messages[0] if len(messages) == 1 else messages}, status=status.HTTP_400_BAD_REQUEST)


def _not_found():
    return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)


def _denied(request, permission, relationship, entity_type, entity_id):
    """Audits the denial and returns the 403, or None when the caller holds the permission."""
    if has_permission(request.user_account, permission, relationship=relationship):
        return None
    write_audit(
        action=f"{permission}.denied",
        entity_type=entity_type,
        entity_id=entity_id,
        request=request,
        metadata={"relationship_id": str(relationship.id)},
    )
    return Response({"detail": "Not permitted.", "code": f"{permission.replace('.', '_')}_denied"},
                    status=status.HTTP_403_FORBIDDEN)


def _relationship(pk):
    return SupervisoryRelationship.objects.select_related("supervisor", "supervisee", "organization").filter(pk=pk).first()


def _cycles(relationship):
    return (
        MonthlyCycle.objects.filter(relationship=relationship)
        .select_related("relationship")
        .prefetch_related(
            "assignments__attachments",
            "assignments__issued_by",
            "hours_entries__logged_by",
            "hours_entries__verified_by",
            "sessions__documented_by",
            "service_attestations__attested_by",
        )
        .order_by("year", "month")
    )


def _assignment(pk):
    return (
        Assignment.objects.select_related(
            "cycle", "cycle__relationship", "cycle__relationship__supervisor",
            "cycle__relationship__supervisee", "issued_by",
        )
        .prefetch_related("events__actor", "attachments__uploaded_by")
        .filter(pk=pk)
        .first()
    )


def _assignment_response(pk, code=status.HTTP_200_OK):
    return Response(monthly.serialize_assignment(_assignment(pk), detail=True), status=code)


# ---------------------------------------------------------------------------
# Cycles
# ---------------------------------------------------------------------------

class CycleListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Every monthly cycle of a relationship, with counts and compliance", operation_id="cycles_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "cycle.view", rel, "monthly_cycle", "list"):
            return denied
        cycles = list(_cycles(rel))
        summary = relationship_compliance(rel, cycles)
        by_cycle = {month["cycle_id"]: month for month in summary["months"]}
        return Response({
            "relationship_id": str(rel.id),
            "track": rel.supervision_track,
            "can_manage": has_permission(request.user_account, "cycle.manage", relationship=rel),
            "cycles": [monthly.serialize_cycle(c, compliance=by_cycle.get(str(c.id))) for c in reversed(cycles)],
            "cumulative": summary["cumulative"],
        })

    @extend_schema(tags=["Monthly Work"], summary="Open a monthly cycle", request=CycleCreateSerializer, operation_id="cycles_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "cycle.manage", rel, "monthly_cycle", "new"):
            return denied
        serializer = CycleCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            cycle = monthly.create_cycle(rel, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="create", entity_type="monthly_cycle", entity_id=cycle.id, request=request,
                    after={"year": cycle.year, "month": cycle.month, "fieldwork_type": cycle.fieldwork_type})
        cycle = _cycles(rel).get(pk=cycle.pk)
        return Response(monthly.serialize_cycle(cycle, compliance=cycle_compliance(cycle), detail=True),
                        status=status.HTTP_201_CREATED)


class CycleDetailView(APIView):
    permission_classes = [HasWorkspace]

    def _load(self, pk):
        cycle = MonthlyCycle.objects.select_related("relationship").filter(pk=pk).first()
        return None if cycle is None else _cycles(cycle.relationship).get(pk=pk)

    @extend_schema(tags=["Monthly Work"], summary="One cycle: its assignments, hours and sessions", operation_id="cycles_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        cycle = self._load(pk)
        if cycle is None:
            return _not_found()
        if denied := _denied(request, "cycle.view", cycle.relationship, "monthly_cycle", cycle.id):
            return denied
        return Response(monthly.serialize_cycle(cycle, compliance=cycle_compliance(cycle), detail=True))

    @extend_schema(tags=["Monthly Work"], summary="Set a fieldwork month to supervised or concentrated", request=CyclePatchSerializer, operation_id="cycles_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        cycle = self._load(pk)
        if cycle is None:
            return _not_found()
        if denied := _denied(request, "cycle.manage", cycle.relationship, "monthly_cycle", cycle.id):
            return denied
        serializer = CyclePatchSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        before = cycle.fieldwork_type
        try:
            monthly.set_cycle_fieldwork_type(cycle, serializer.validated_data["fieldwork_type"])
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="update", entity_type="monthly_cycle", entity_id=cycle.id, request=request,
                    before={"fieldwork_type": before}, after={"fieldwork_type": cycle.fieldwork_type})
        cycle = self._load(pk)
        return Response(monthly.serialize_cycle(cycle, compliance=cycle_compliance(cycle), detail=True))


class ComplianceView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Supervision percentage and cumulative hours for a relationship", operation_id="relationship_compliance", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "compliance.view", rel, "compliance", "view"):
            return denied
        return Response(relationship_compliance(rel, list(_cycles(rel))))


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

class AssignmentListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Assignments"], summary="Assignments of a relationship (optionally one month)", operation_id="assignments_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "cycle.view", rel, "assignment", "list"):
            return denied
        qs = (
            Assignment.objects.filter(cycle__relationship=rel, record_status="active")
            .select_related("cycle", "issued_by")
            .prefetch_related("attachments")
            .order_by("-cycle__year", "-cycle__month", "created_at")
        )
        if request.query_params.get("year") and request.query_params.get("month"):
            qs = qs.filter(cycle__year=request.query_params["year"], cycle__month=request.query_params["month"])
        return Response({
            "can_issue": has_permission(request.user_account, "assignment.create", relationship=rel),
            "can_submit": has_permission(request.user_account, "assignment.submit", relationship=rel),
            "can_review": has_permission(request.user_account, "assignment.review", relationship=rel),
            "assignments": [monthly.serialize_assignment(a) for a in qs],
        })

    @extend_schema(tags=["Assignments"], summary="Issue an assignment", request=AssignmentCreateSerializer, operation_id="assignments_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "assignment.create", rel, "assignment", "new"):
            return denied
        serializer = AssignmentCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            assignment = monthly.issue_assignment(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="assignment.issue", entity_type="assignment", entity_id=assignment.id, request=request,
                    after={"title": assignment.title, "cycle_id": str(assignment.cycle_id)})
        return _assignment_response(assignment.id, status.HTTP_201_CREATED)


class AssignmentDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Assignments"], summary="One assignment with every recorded state and its files", operation_id="assignments_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        assignment = _assignment(pk)
        if assignment is None:
            return _not_found()
        rel = assignment.cycle.relationship
        if denied := _denied(request, "cycle.view", rel, "assignment", assignment.id):
            return denied
        data = monthly.serialize_assignment(assignment, detail=True)
        data["can_submit"] = has_permission(request.user_account, "assignment.submit", relationship=rel)
        data["can_review"] = has_permission(request.user_account, "assignment.review", relationship=rel)
        return Response(data)


class _AssignmentActionView(APIView):
    permission_classes = [HasWorkspace]
    permission = None
    action = None

    def run(self, assignment, request):
        raise NotImplementedError

    def post(self, request, pk):
        assignment = _assignment(pk)
        if assignment is None:
            return _not_found()
        if denied := _denied(request, self.permission, assignment.cycle.relationship, "assignment", assignment.id):
            return denied
        before = assignment.status
        try:
            response = self.run(assignment, request)
        except ValidationError as exc:
            return _bad_request(exc)
        if response is not None:
            return response
        write_audit(action=self.action, entity_type="assignment", entity_id=assignment.id, request=request,
                    before={"status": before}, after={"status": assignment.status})
        return _assignment_response(assignment.id)


class AssignmentSubmitView(_AssignmentActionView):
    permission = "assignment.submit"
    action = "assignment.submit"
    parser_classes = [MultiPartParser, FormParser, JSONParser]

    @extend_schema(tags=["Assignments"], summary="Submit (or resubmit) an assignment with attachments", request={"multipart/form-data": AssignmentSubmitSerializer}, operation_id="assignments_submit", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, assignment, request):
        monthly.submit_assignment(
            assignment, request.user_account,
            note=request.data.get("note") or "",
            files=request.FILES.getlist("files"),
        )


class AssignmentRevisionView(_AssignmentActionView):
    permission = "assignment.review"
    action = "assignment.request_revision"

    @extend_schema(tags=["Assignments"], summary="Request a revision of a submitted assignment", request=AssignmentReviewSerializer, operation_id="assignments_request_revision", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, assignment, request):
        monthly.request_revision(assignment, request.user_account, comment=request.data.get("comment") or "")


class AssignmentCompleteView(_AssignmentActionView):
    permission = "assignment.review"
    action = "assignment.complete"

    @extend_schema(tags=["Assignments"], summary="Mark a submitted assignment complete", request=AssignmentReviewSerializer, operation_id="assignments_complete", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)

    def run(self, assignment, request):
        monthly.complete_assignment(assignment, request.user_account, comment=request.data.get("comment") or "")


class AttachmentDownloadView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Assignments"], summary="Download a submitted file", operation_id="assignment_attachment_download", responses={200: bytes, 403: dict, 404: dict})
    def get(self, request, pk):
        attachment = (
            AssignmentAttachment.objects.select_related(
                "assignment__cycle__relationship__supervisor", "assignment__cycle__relationship__supervisee"
            )
            .filter(pk=pk)
            .first()
        )
        if attachment is None:
            return _not_found()
        if denied := _denied(request, "cycle.view", attachment.assignment.cycle.relationship,
                             "assignment_attachment", attachment.id):
            return denied
        try:
            handle = attachment.file.open("rb")
        except (FileNotFoundError, OSError):
            return Response({"detail": "The file is no longer in storage.", "code": "file_missing"},
                            status=status.HTTP_404_NOT_FOUND)
        return FileResponse(handle, as_attachment=True, filename=attachment.original_name,
                            content_type=attachment.content_type or "application/octet-stream")


# ---------------------------------------------------------------------------
# Model 1 — Supervised Fieldwork hours
# ---------------------------------------------------------------------------

def _hours_queryset():
    return HoursEntry.objects.select_related(
        "cycle__relationship__supervisor", "cycle__relationship__supervisee", "logged_by", "verified_by"
    )


class HoursListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Hours"], summary="Self-logged fieldwork hours of a relationship", operation_id="hours_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "cycle.view", rel, "hours_entry", "list"):
            return denied
        qs = _hours_queryset().filter(cycle__relationship=rel, record_status="active").order_by("-occurred_on", "-created_at")
        if request.query_params.get("year") and request.query_params.get("month"):
            qs = qs.filter(cycle__year=request.query_params["year"], cycle__month=request.query_params["month"])
        return Response({
            "model": "fieldwork_verified_hours",
            "applies": rel.supervision_track in monthly.SELF_LOGGED_HOURS_TRACKS,
            "can_log": has_permission(request.user_account, "hours.log", relationship=rel),
            "can_verify": has_permission(request.user_account, "hours.verify", relationship=rel),
            "entries": [monthly.serialize_hours(entry) for entry in qs],
        })

    @extend_schema(tags=["Hours"], summary="Log fieldwork hours (Supervisee)", request=HoursCreateSerializer, operation_id="hours_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "hours.log", rel, "hours_entry", "new"):
            return denied
        serializer = HoursCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            entry = monthly.log_fieldwork_hours(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="hours.log", entity_type="hours_entry", entity_id=entry.id, request=request,
                    after=monthly.serialize_hours(entry))
        return Response(monthly.serialize_hours(entry), status=status.HTTP_201_CREATED)


class HoursDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Hours"], summary="Correct a pending or returned entry (Supervisee); it goes back for verification", request=HoursPatchSerializer, operation_id="hours_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        entry = _hours_queryset().filter(pk=pk).first()
        if entry is None:
            return _not_found()
        if denied := _denied(request, "hours.log", entry.cycle.relationship, "hours_entry", entry.id):
            return denied
        serializer = HoursPatchSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _invalid(serializer)
        before = monthly.serialize_hours(entry)
        try:
            monthly.correct_fieldwork_hours(entry, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="hours.correct", entity_type="hours_entry", entity_id=entry.id, request=request,
                    before=before, after=monthly.serialize_hours(entry))
        return Response(monthly.serialize_hours(entry))


class HoursVerifyView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Hours"], summary="Verify logged hours (Supervisor)", request=None, operation_id="hours_verify", responses=JSON_RESPONSE)
    def post(self, request, pk):
        entry = _hours_queryset().filter(pk=pk).first()
        if entry is None:
            return _not_found()
        if denied := _denied(request, "hours.verify", entry.cycle.relationship, "hours_entry", entry.id):
            return denied
        try:
            monthly.verify_fieldwork_hours(entry, request.user_account)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="hours.verify", entity_type="hours_entry", entity_id=entry.id, request=request,
                    after={"status": entry.status})
        return Response(monthly.serialize_hours(entry))


class HoursReturnView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Hours"], summary="Return logged hours for correction (Supervisor)", request=HoursReturnSerializer, operation_id="hours_return", responses=JSON_RESPONSE)
    def post(self, request, pk):
        entry = _hours_queryset().filter(pk=pk).first()
        if entry is None:
            return _not_found()
        if denied := _denied(request, "hours.verify", entry.cycle.relationship, "hours_entry", entry.id):
            return denied
        try:
            monthly.return_fieldwork_hours(entry, request.user_account, reason=request.data.get("reason") or "")
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="hours.return", entity_type="hours_entry", entity_id=entry.id, request=request,
                    after={"status": entry.status, "return_reason": entry.return_reason})
        return Response(monthly.serialize_hours(entry))


# ---------------------------------------------------------------------------
# Model 2 — Supervisor-authored sessions and self-attested service hours
# ---------------------------------------------------------------------------

class SessionListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Sessions"], summary="Documented supervision sessions of a relationship", operation_id="sessions_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "session.view", rel, "supervision_session", "list"):
            return denied
        qs = (
            SupervisionSession.objects.filter(cycle__relationship=rel, record_status="active")
            .select_related("documented_by")
            .order_by("-occurred_on", "-created_at")
        )
        if request.query_params.get("year") and request.query_params.get("month"):
            qs = qs.filter(cycle__year=request.query_params["year"], cycle__month=request.query_params["month"])
        return Response({
            "can_document": has_permission(request.user_account, "session.document", relationship=rel),
            "sessions": [monthly.serialize_session(s) for s in qs],
        })

    @extend_schema(tags=["Sessions"], summary="Document a supervision session (Supervisor)", request=SessionCreateSerializer, operation_id="sessions_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "session.document", rel, "supervision_session", "new"):
            return denied
        serializer = SessionCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            session = monthly.document_session(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="session.document", entity_type="supervision_session", entity_id=session.id,
                    request=request, after=monthly.serialize_session(session))
        return Response(monthly.serialize_session(session), status=status.HTTP_201_CREATED)


class ServiceHoursView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Sessions"], summary="Attest this month's service hours (RBT / BCaBA Supervisee)", request=ServiceHoursSerializer, operation_id="service_hours_attest", responses=JSON_RESPONSE)
    def put(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "service_hours.attest", rel, "service_hours_attestation", "new"):
            return denied
        serializer = ServiceHoursSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            attestation = monthly.attest_service_hours(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="service_hours.attest", entity_type="service_hours_attestation",
                    entity_id=attestation.id, request=request, after=monthly.serialize_attestation(attestation))
        return Response(monthly.serialize_attestation(attestation))
