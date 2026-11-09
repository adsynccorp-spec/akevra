from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.supervision import closure, notifications
from apps.supervision.models import ConclusionReason
from apps.supervision.monthly_views import _bad_request, _denied, _not_found, _relationship


class ConclusionReasonsView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Supervision"], summary="Reasons a relationship can be concluded", operation_id="conclusion_reasons", responses=JSON_RESPONSE)
    def get(self, request):
        return Response([{"value": v, "label": label} for v, label in ConclusionReason.choices])


class ConcludeRelationshipView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Supervision"], summary="Conclude a relationship with a reason (it stays fully readable)", operation_id="relationship_conclude", responses=JSON_RESPONSE)
    def post(self, request, pk):
        from apps.supervision.views import serialize_relationship

        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "relationship.conclude", rel, "supervisory_relationship", rel.id):
            return denied
        try:
            conclusion = closure.conclude(rel, request.user_account, reason=request.data.get("reason") or "",
                                          concluded_on=request.data.get("concluded_on") or None, notes=request.data.get("notes") or "")
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action="relationship.conclude", entity_type="supervisory_relationship", entity_id=rel.id, request=request,
                    before={"status": "active"}, after={"status": rel.status, "ended_on": rel.ended_on.isoformat(),
                                                        "reason": conclusion.reason, "notes": conclusion.notes})
        other = rel.supervisee if request.user_account.id == rel.supervisor_id else rel.supervisor
        notifications.notify(other, "relationship_concluded", f"Relationship with {request.user_account.full_name} concluded",
                             body=f"Reason: {ConclusionReason(conclusion.reason).label}. All records stay available to view.",
                             relationship=rel, tab="overview")
        rel.refresh_from_db()
        return Response(serialize_relationship(rel, request.user_account))


class RelationshipExportView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Reports"], summary="Compliance & audit export of a relationship (CSV or JSON)", operation_id="relationship_export", responses=JSON_RESPONSE)
    def get(self, request, pk):
        from django.http import HttpResponse

        from apps.supervision import exports

        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "reports.export", rel, "supervisory_relationship", rel.id):
            return denied
        data = exports.build(rel)
        write_audit(action="relationship.export", entity_type="supervisory_relationship", entity_id=rel.id, request=request,
                    metadata={"format": request.query_params.get("as", "csv")})
        if request.query_params.get("as") == "json":
            return Response(data)
        name = f"akevra-{rel.supervisee.last_name or 'relationship'}-{rel.supervision_track}.csv".lower().replace(" ", "-")
        response = HttpResponse(exports.to_csv(data), content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{name}"'
        return response
