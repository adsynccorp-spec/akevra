from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.models import AuditEvent
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission


class AuditListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Recent audit events", responses=JSON_RESPONSE)
    def get(self, request):
        if not has_permission(request.user_account, "audit.view"):
            return Response({"detail": "Not permitted."}, status=403)
        events = AuditEvent.objects.filter(organization=request.organization)[:100]
        return Response([
            {
                "id": str(e.id),
                "action": e.action,
                "entity_type": e.entity_type,
                "entity_id": e.entity_id,
                "created_at": e.created_at.isoformat(),
                "metadata": e.metadata,
            }
            for e in events
        ])
