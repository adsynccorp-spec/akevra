"""Competency assessments API: Draft -> Ready for Review -> Official (Supervisor); others see official ones."""

from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE, AssessmentWriteSerializer
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission
from apps.supervision import assessments, notifications
from apps.supervision.models import AssessmentStatus, CompetencyAssessment
from apps.supervision.monthly_views import _bad_request, _denied, _invalid, _not_found, _relationship


def _queryset():
    return CompetencyAssessment.objects.filter(record_status="active").select_related(
        "relationship__supervisor", "relationship__supervisee", "plan_competency", "related_goal", "assessed_by"
    )


class AssessmentListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Competency Assessments"], summary="Competency assessments of a relationship", operation_id="assessments_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "assessment.view", rel, "competency_assessment", "list"):
            return denied
        can_manage = has_permission(request.user_account, "assessment.manage", relationship=rel)
        rows = list(_queryset().filter(relationship=rel))
        visible = rows if can_manage else [a for a in rows if a.status == AssessmentStatus.OFFICIAL]
        return Response({
            "can_manage": can_manage,
            "summary": assessments.summarize(rows),
            "assessments": [assessments.serialize_assessment(a) for a in visible],
        })

    @extend_schema(tags=["Competency Assessments"], summary="Start a competency assessment (Supervisor)", request=AssessmentWriteSerializer, operation_id="assessments_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "assessment.manage", rel, "competency_assessment", "new"):
            return denied
        serializer = AssessmentWriteSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            assessment = assessments.create_assessment(rel, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="assessment.create", entity_type="competency_assessment", entity_id=assessment.id, request=request)
        return Response(assessments.serialize_assessment(_queryset().get(pk=assessment.pk)), status=status.HTTP_201_CREATED)


class AssessmentDetailView(APIView):
    permission_classes = [HasWorkspace]

    def _load(self, request, pk, permission):
        assessment = _queryset().filter(pk=pk).first()
        if assessment is None:
            return None, _not_found()
        rel = assessment.relationship
        if denied := _denied(request, permission, rel, "competency_assessment", assessment.id):
            return None, denied
        if assessment.status != AssessmentStatus.OFFICIAL and not has_permission(request.user_account, "assessment.manage", relationship=rel):
            return None, _not_found()
        return assessment, None

    @extend_schema(tags=["Competency Assessments"], summary="One competency assessment", operation_id="assessments_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        assessment, error = self._load(request, pk, "assessment.view")
        if error:
            return error
        return Response(assessments.serialize_assessment(assessment))

    @extend_schema(tags=["Competency Assessments"], summary="Edit a draft or in-review assessment (Supervisor)", request=AssessmentWriteSerializer, operation_id="assessments_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        assessment, error = self._load(request, pk, "assessment.manage")
        if error:
            return error
        serializer = AssessmentWriteSerializer(data=request.data, partial=True)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            assessments.update_assessment(assessment, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="assessment.update", entity_type="competency_assessment", entity_id=assessment.id, request=request)
        return Response(assessments.serialize_assessment(_queryset().get(pk=pk)))


class _AssessmentAction(APIView):
    permission_classes = [HasWorkspace]
    action = None
    run = None

    def post(self, request, pk):
        assessment = _queryset().filter(pk=pk).first()
        if assessment is None:
            return _not_found()
        if denied := _denied(request, "assessment.manage", assessment.relationship, "competency_assessment", assessment.id):
            return denied
        before = assessment.status
        try:
            type(self).run(assessment)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action=f"assessment.{self.action}", entity_type="competency_assessment", entity_id=assessment.id,
                    request=request, before={"status": before}, after={"status": assessment.status})
        if assessment.status == AssessmentStatus.OFFICIAL:
            notifications.assessment_finalized(assessment, request.user_account)
        return Response(assessments.serialize_assessment(_queryset().get(pk=pk)))


class AssessmentSubmitView(_AssessmentAction):
    action = "submit"
    run = staticmethod(assessments.submit_for_review)

    @extend_schema(tags=["Competency Assessments"], summary="Send a draft for review", request=None, operation_id="assessments_submit", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)


class AssessmentReturnView(_AssessmentAction):
    action = "return"
    run = staticmethod(assessments.return_to_draft)

    @extend_schema(tags=["Competency Assessments"], summary="Return an in-review assessment to draft", request=None, operation_id="assessments_return", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)


class AssessmentFinalizeView(_AssessmentAction):
    action = "finalize"
    run = staticmethod(assessments.finalize)

    @extend_schema(tags=["Competency Assessments"], summary="Finalize as the official assessment", request=None, operation_id="assessments_finalize", responses=JSON_RESPONSE)
    def post(self, request, pk):
        return super().post(request, pk)
