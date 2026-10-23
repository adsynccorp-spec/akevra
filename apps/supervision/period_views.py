from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils import timezone
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import UserAccount
from apps.audit.services import write_audit
from apps.core.api_serializers import (
    JSON_RESPONSE, CertificationSerializer, CoordinatorSerializer, CycleStatusSerializer, ExternalSupervisionSerializer,
)
from apps.rbac.models import OrgRole
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import org_roles_for
from apps.supervision import audit_log, periods
from apps.supervision.models import (
    ExternalSupervisionRecord,
    MonthlyCycle,
    RequirementsCoordinatorDesignation,
    ServiceHoursRecord,
    SupervisoryRelationship,
)
from apps.supervision.monthly_views import _bad_request, _denied, _invalid, _not_found


def _forbidden():
    return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)


def _supervisee_access(request, supervisee_id):
    """(supervisee, can_view, can_manage). Visible to the supervisee, their supervisors and
    organization oversight; managed by their supervisors and Administrators / Clinical Directors."""
    me = request.user_account
    supervisee = UserAccount.objects.filter(pk=supervisee_id, organization=me.organization).first()
    if supervisee is None:
        return None, False, False
    oversight = bool(set(org_roles_for(me)) & {OrgRole.ADMINISTRATOR, OrgRole.CLINICAL_DIRECTOR})
    supervises = SupervisoryRelationship.objects.filter(supervisee=supervisee, supervisor=me).exists()
    return supervisee, oversight or supervises or me.id == supervisee.id, oversight or supervises


class CycleStatusView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Change a month's status: open, ready_for_review, closed (verified); reopening needs a reason",
                   request=CycleStatusSerializer, operation_id="cycles_status", responses=JSON_RESPONSE)
    def post(self, request, pk):
        cycle = MonthlyCycle.objects.select_related("relationship__supervisee", "relationship__supervisor").filter(pk=pk).first()
        if cycle is None:
            return _not_found()
        if denied := _denied(request, "cycle.manage", cycle.relationship, "monthly_cycle", cycle.id):
            return denied
        serializer = CycleStatusSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        before = cycle.status
        try:
            periods.change_status(cycle, request.user_account, serializer.validated_data["status"], serializer.validated_data.get("reason", ""))
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="cycle.status", entity_type="monthly_cycle", entity_id=cycle.id, request=request,
                    before={"status": before}, after={"status": cycle.status, "reason": serializer.validated_data.get("reason", "")})
        from apps.supervision.monthly_views import CycleDetailView
        return CycleDetailView().get(request, pk)


class SuperviseeServiceHoursView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="The supervisee's shared monthly service-hours records, including superseded corrections",
                   operation_id="supervisee_service_hours", responses=JSON_RESPONSE)
    def get(self, request, pk):
        supervisee, can_view, _ = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not can_view:
            return _forbidden()
        rows = ServiceHoursRecord.objects.filter(supervisee=supervisee).exclude(record_status="archived") \
            .select_related("entered_by", "reviewed_by").order_by("-year", "-month", "-created_at")
        through = periods.last_confirmed_through(supervisee.id)
        return Response({
            "records": [periods.serialize_service_hours(r) for r in rows],
            "last_confirmed_through": through.isoformat() if through else None,
        })


class ServiceHoursReviewView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Mark the supervisee's service hours reviewed (supervisor)", request=None,
                   operation_id="service_hours_review", responses=JSON_RESPONSE)
    def post(self, request, pk):
        record = ServiceHoursRecord.objects.select_related("supervisee", "entered_by").filter(pk=pk).first()
        if record is None:
            return _not_found()
        _, _, can_manage = _supervisee_access(request, record.supervisee_id)
        if not can_manage:
            return _forbidden()
        try:
            rel = SupervisoryRelationship.objects.filter(pk=request.data.get("relationship_id")).first() if request.data.get("relationship_id") else \
                SupervisoryRelationship.objects.filter(supervisee_id=record.supervisee_id, supervisor=request.user_account).order_by("-started_on").first()
            periods.review_service_hours(record, request.user_account, rel)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="service_hours.review", entity_type="service_hours_record", entity_id=record.id, request=request)
        return Response(periods.serialize_service_hours(record))


def _serialize_designation(d):
    return {
        "id": str(d.id),
        "coordinator": {"id": str(d.coordinator_id), "name": d.coordinator.full_name, "certification_number": d.coordinator.certification_number},
        "starts_on": d.starts_on.isoformat(),
        "ends_on": d.ends_on.isoformat() if d.ends_on else None,
        "active": d.starts_on <= timezone.localdate() and (d.ends_on is None or d.ends_on >= timezone.localdate()),
    }


class CoordinatorListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="RBT Requirements Coordinator designations for a supervisee", operation_id="coordinators_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        supervisee, can_view, can_manage = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not can_view:
            return _forbidden()
        rows = RequirementsCoordinatorDesignation.objects.filter(supervisee=supervisee, record_status="active").select_related("coordinator")
        return Response({"can_manage": can_manage, "designations": [_serialize_designation(d) for d in rows]})

    @extend_schema(tags=["Audit"], summary="Designate an RBT Requirements Coordinator (dated)", request=CoordinatorSerializer, operation_id="coordinators_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        supervisee, _, can_manage = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not can_manage:
            return _forbidden()
        serializer = CoordinatorSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        coordinator = UserAccount.objects.filter(pk=serializer.validated_data["coordinator_id"], organization=supervisee.organization).first()
        if coordinator is None:
            return Response({"detail": "Choose a person in this organization."}, status=status.HTTP_400_BAD_REQUEST)
        try:
            designation = RequirementsCoordinatorDesignation.objects.create(
                organization=supervisee.organization, supervisee=supervisee, coordinator=coordinator,
                starts_on=serializer.validated_data["starts_on"], ends_on=serializer.validated_data.get("ends_on"),
                created_by=request.user_account,
            )
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="coordinator.designate", entity_type="requirements_coordinator_designation", entity_id=designation.id, request=request)
        return Response(_serialize_designation(designation), status=status.HTTP_201_CREATED)


class CoordinatorDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Set the end date of a designation", request=CoordinatorSerializer, operation_id="coordinators_update", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        designation = RequirementsCoordinatorDesignation.objects.select_related("coordinator").filter(pk=pk, record_status="active").first()
        if designation is None:
            return _not_found()
        _, _, can_manage = _supervisee_access(request, designation.supervisee_id)
        if not can_manage:
            return _forbidden()
        if "ends_on" in request.data:
            designation.ends_on = request.data.get("ends_on") or None
        try:
            designation.save()
        except ValidationError as exc:
            return _bad_request(exc)
        designation.refresh_from_db()
        write_audit(action="coordinator.update", entity_type="requirements_coordinator_designation", entity_id=designation.id, request=request)
        return Response(_serialize_designation(designation))


class CertificationView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Set a person's BACB certification number (themselves or an Administrator)", request=CertificationSerializer,
                   operation_id="people_certification", responses=JSON_RESPONSE)
    def patch(self, request, pk):
        me = request.user_account
        person = UserAccount.objects.filter(pk=pk, organization=me.organization).first()
        if person is None:
            return _not_found()
        if person.id != me.id and OrgRole.ADMINISTRATOR not in org_roles_for(me):
            return _forbidden()
        serializer = CertificationSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        before = person.certification_number
        person.certification_number = serializer.validated_data["certification_number"].strip()
        person.save(update_fields=["certification_number", "updated_at"])
        write_audit(action="person.certification", entity_type="user_account", entity_id=person.id, request=request,
                    before={"certification_number": before}, after={"certification_number": person.certification_number})
        return Response({"id": str(person.id), "certification_number": person.certification_number})


class AuditLogView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(
        tags=["Audit"], summary="Month-by-month supervision audit log for a supervisee (RBT / BCaBA), with flags and traceable records",
        parameters=[
            OpenApiParameter("start", OpenApiTypes.STR, description="First month, YYYY-MM (default: 12 months ago)"),
            OpenApiParameter("months", OpenApiTypes.INT, description="Number of months (1–36, default 12)"),
            OpenApiParameter("pathway", OpenApiTypes.STR, description="rbt or bcaba (default from the supervisee's credential)"),
            OpenApiParameter("export", OpenApiTypes.STR, description="csv to download the BACB RBT log columns"),
        ],
        operation_id="audit_log", responses=JSON_RESPONSE,
    )
    def get(self, request, pk):
        supervisee, can_view, _ = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not can_view:
            return _forbidden()
        today = timezone.localdate()
        try:
            months = max(1, min(36, int(request.query_params.get("months", 12))))
            if request.query_params.get("start"):
                start_year, start_month = (int(x) for x in request.query_params["start"].split("-"))
                if not 1 <= start_month <= 12:
                    raise ValueError
            else:
                total = today.year * 12 + today.month - 1 - months
                start_year, start_month = total // 12, total % 12 + 1
            # A From / To range (inclusive) overrides the month count
            if request.query_params.get("end"):
                end_year, end_month = (int(x) for x in request.query_params["end"].split("-"))
                span = (end_year * 12 + end_month) - (start_year * 12 + start_month) + 1
                if not 1 <= end_month <= 12 or span < 1:
                    return Response({"detail": "The To month must be on or after the From month."}, status=status.HTTP_400_BAD_REQUEST)
                if span > 36:
                    return Response({"detail": "Choose a range of at most 36 months."}, status=status.HTTP_400_BAD_REQUEST)
                months = span
        except ValueError:
            return Response({"detail": "Use start=YYYY-MM (and end=YYYY-MM or months=N)."}, status=status.HTTP_400_BAD_REQUEST)
        pathway = request.query_params.get("pathway") or ("bcaba" if supervisee.credential_type == "bcaba" else "rbt")
        if pathway not in audit_log.PATHWAYS:
            return Response({"detail": "pathway must be rbt or bcaba."}, status=status.HTTP_400_BAD_REQUEST)
        log = audit_log.build(supervisee, pathway=pathway, start_year=start_year, start_month=start_month, months=months)
        write_audit(action="audit_log.view", entity_type="user_account", entity_id=supervisee.id, request=request,
                    metadata={"start": f"{start_year}-{start_month:02d}", "months": months, "export": request.query_params.get("export", "")})
        if request.query_params.get("export") == "csv":
            response = HttpResponse(audit_log.to_csv(log), content_type="text/csv")
            name = supervisee.full_name.replace(" ", "_")
            response["Content-Disposition"] = f'attachment; filename="{pathway.upper()}_Supervision_Audit_Log_{name}_{start_year}-{start_month:02d}.csv"'
            return response
        return Response(log)


class AuditPeopleView(APIView):
    """Supervisees the caller can build an audit log for (RBT / BCaBA pathways)."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Supervisees available for the audit log", operation_id="audit_people", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        oversight = bool(set(org_roles_for(me)) & {OrgRole.ADMINISTRATOR, OrgRole.CLINICAL_DIRECTOR})
        rels = SupervisoryRelationship.objects.filter(supervision_track__in=list(audit_log.PATHWAYS.values())).select_related("supervisee")
        if not oversight:
            rels = rels.filter(supervisor=me) | rels.filter(supervisee=me)
        people = {}
        for r in rels:
            entry = people.setdefault(r.supervisee_id, {"id": str(r.supervisee_id), "name": r.supervisee.full_name,
                                                        "credential_type": r.supervisee.credential_type,
                                                        "certification_number": r.supervisee.certification_number, "pathways": set()})
            entry["pathways"].add("bcaba" if r.supervision_track == audit_log.PATHWAYS["bcaba"] else "rbt")
        can_manage = oversight or SupervisoryRelationship.objects.filter(supervisor=me).exists()
        # The people directory (for coordinator designations) is only for those who can manage;
        # a supervisee viewing their own log gets just their own record
        directory = UserAccount.objects.filter(organization=me.organization, record_status="active").order_by("first_name") if can_manage else []
        return Response({
            "supervisees": [{**p, "pathways": sorted(p["pathways"])} for p in sorted(people.values(), key=lambda p: p["name"])],
            "is_administrator": OrgRole.ADMINISTRATOR in org_roles_for(me),
            "me": str(me.id),
            "directory": [{"id": str(u.id), "name": u.full_name, "credential_type": u.credential_type,
                           "certification_number": u.certification_number} for u in directory],
            "can_manage": can_manage,
            "own_only": not can_manage,
        })


class ExternalSupervisionView(APIView):
    """Supervision received at another agency: self-reported by the supervisee or entered for them.
    Part of the supervisee's overall monthly picture, never of this organization's requirement."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="External (another agency) supervision records for a supervisee", operation_id="external_supervision_list", responses=JSON_RESPONSE)
    def get(self, request, pk):
        supervisee, can_view, _ = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not can_view:
            return _forbidden()
        rows = ExternalSupervisionRecord.objects.filter(supervisee=supervisee, record_status="active").select_related("entered_by", "reviewed_by").order_by("-occurred_on")
        return Response({"records": [periods.serialize_external_supervision(r) for r in rows]})

    @extend_schema(tags=["Monthly Work"], summary="Report supervision received at another agency", request=ExternalSupervisionSerializer, operation_id="external_supervision_create", responses=JSON_RESPONSE)
    def post(self, request, pk):
        supervisee, can_view, can_manage = _supervisee_access(request, pk)
        if supervisee is None:
            return _not_found()
        if not (can_manage or request.user_account.id == supervisee.id):
            return _forbidden()
        serializer = ExternalSupervisionSerializer(data=request.data)
        if not serializer.is_valid():
            return _invalid(serializer)
        try:
            row = periods.record_external_supervision(supervisee, request.user_account, **serializer.validated_data)
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="external_supervision.record", entity_type="external_supervision_record", entity_id=row.id, request=request)
        return Response(periods.serialize_external_supervision(row), status=status.HTTP_201_CREATED)


class ServiceHoursPeriodView(APIView):
    """Service hours since the last confirmed entry, for a date range within one month (Supervisee)."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Monthly Work"], summary="Add service hours for a date range since the last confirmed entry", request=None,
                   operation_id="service_hours_period", responses=JSON_RESPONSE)
    def post(self, request, pk):
        from datetime import date as _date

        from apps.supervision.monthly_views import _denied, _relationship

        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "service_hours.attest", rel, "service_hours_period", "new"):
            return denied
        try:
            start, end = _date.fromisoformat(str(request.data.get("start_date"))), _date.fromisoformat(str(request.data.get("end_date")))
        except ValueError:
            return Response({"detail": "Give start_date and end_date as YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        if rel.supervisee_id != request.user_account.id:
            return _forbidden()
        try:
            entry, record = periods.record_service_period(rel, request.user_account, start_date=start, end_date=end,
                                                          hours=request.data.get("hours"), zero_reason=request.data.get("zero_reason") or "",
                                                          notes=request.data.get("notes") or "")
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="service_hours.period", entity_type="service_hours_period_entry", entity_id=entry.id, request=request,
                    after={"start": start.isoformat(), "end": end.isoformat(), "hours": str(entry.hours), "month_total": str(record.hours)})
        through = periods.last_confirmed_through(rel.supervisee_id)
        return Response({"period": periods.serialize_period(entry), "record": periods.serialize_service_hours(record),
                         "last_confirmed_through": through.isoformat() if through else None}, status=status.HTTP_201_CREATED)
