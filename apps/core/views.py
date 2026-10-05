from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace


class HealthView(APIView):
    authentication_classes = []
    permission_classes = []

    @extend_schema(tags=["System"], summary="Health check", auth=[], responses=JSON_RESPONSE)
    def get(self, request):
        return Response({"status": "ok", "service": "akevra"})


class CurrentOrganizationView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["System"], summary="Current organization", responses=JSON_RESPONSE)
    def get(self, request):
        org = request.organization
        return Response({"id": str(org.id), "name": org.name, "slug": org.slug})
