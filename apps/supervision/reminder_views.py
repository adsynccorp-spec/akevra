"""Shortfall reminders API. Reading refreshes them from the current results; acting on them never
changes compliance data."""

from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission
from apps.supervision import reminders
from apps.supervision.models import ComplianceReminder
from apps.supervision.monthly_views import _bad_request, _denied, _not_found, _relationship, _relationships_with


def _rows(qs, me):
    rows = qs.select_related("cycle__relationship__supervisor", "cycle__relationship__supervisee", "resolved_by").order_by("-cycle__year", "-cycle__month", "created_at")
    return [{**reminders.serialize(r), "can_act": has_permission(me, "cycle.manage", relationship=r.cycle.relationship)} for r in rows]


class RelationshipRemindersView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Compliance"], summary="Shortfall reminders of a relationship", operation_id="relationship_reminders", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "compliance.view", rel, "compliance_reminder", "list"):
            return denied
        reminders.refresh_relationship(rel)
        return Response({"reminders": _rows(ComplianceReminder.objects.filter(cycle__relationship=rel, record_status="active"), request.user_account)})


class AllRemindersView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Compliance"], summary="Shortfall reminders across my relationships", operation_id="reminders_all", responses=JSON_RESPONSE)
    def get(self, request):
        rels = [r for r in _relationships_with(request, "compliance.view") if r.status == "active"]
        for rel in rels:
            reminders.refresh_relationship(rel)
        qs = ComplianceReminder.objects.filter(cycle__relationship__in=rels, record_status="active")
        if request.query_params.get("status"):
            qs = qs.filter(status=request.query_params["status"])
        return Response({"reminders": _rows(qs, request.user_account)})


class ReminderActionView(APIView):
    permission_classes = [HasWorkspace]
    action = None  # resolve | dismiss

    @extend_schema(tags=["Compliance"], summary="Resolve or dismiss a reminder (never changes compliance data)", operation_id="reminder_action", responses=JSON_RESPONSE)
    def post(self, request, pk):
        reminder = ComplianceReminder.objects.select_related("cycle__relationship").filter(pk=pk, record_status="active").first()
        if reminder is None:
            return _not_found()
        rel = reminder.cycle.relationship
        if denied := _denied(request, "cycle.manage", rel, "compliance_reminder", reminder.id):
            return denied
        before = reminder.status
        try:
            reminders.act(reminder, request.user_account, self.action, request.data.get("note") or "")
        except ValidationError as exc:
            return _bad_request(exc)
        write_audit(action=f"reminder.{self.action}", entity_type="compliance_reminder", entity_id=reminder.id, request=request,
                    before={"status": before}, after={"status": reminder.status, "note": reminder.resolution_note})
        return Response({**reminders.serialize(reminder), "can_act": True})
