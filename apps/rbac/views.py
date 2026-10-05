from drf_spectacular.utils import extend_schema, OpenApiParameter
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated

from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.matrix import matrix_as_dict
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission, org_roles_for, relationship_role_for
from apps.supervision.models import SupervisoryRelationship


class MatrixView(APIView):
    permission_classes = [IsAuthenticated, HasWorkspace]

    @extend_schema(tags=["RBAC"], summary="Permission matrix for every approved role", responses=JSON_RESPONSE)
    def get(self, request):
        return Response(matrix_as_dict())


class EvaluateView(APIView):
    permission_classes = [IsAuthenticated, HasWorkspace]

    @extend_schema(
        tags=["RBAC"],
        summary="Evaluate a permission for the current user",
        parameters=[
            OpenApiParameter(name="permission", required=True, type=str),
            OpenApiParameter(name="relationship_id", required=False, type=str),
        ],
        responses=JSON_RESPONSE,
    )
    def get(self, request):
        permission = request.query_params.get("permission")
        relationship_id = request.query_params.get("relationship_id")
        relationship = None
        if relationship_id:
            relationship = SupervisoryRelationship.objects.filter(pk=relationship_id).first()
        allowed = has_permission(request.user_account, permission, relationship=relationship)
        return Response({
            "permission": permission,
            "allowed": allowed,
            "org_roles": sorted(org_roles_for(request.user_account)),
            "relationship_role": relationship_role_for(request.user_account, relationship),
            "relationship_id": str(relationship.id) if relationship else None,
        })
