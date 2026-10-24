from datetime import date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.models import OrgRole
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import org_roles_for
from apps.supervision import rules
from apps.supervision.monthly_views import _bad_request


def _is_admin(request):
    return OrgRole.ADMINISTRATOR in org_roles_for(request.user_account)


class ComplianceRulesView(APIView):
    """Settings → Compliance Rules: review the pathway parameters in force; Administrators approve."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Settings"], summary="Compliance rule sets for this organization (defaults, in force, approval history)", operation_id="compliance_rules", responses=JSON_RESPONSE)
    def get(self, request):
        return Response({"can_approve": _is_admin(request), "tracks": rules.catalog(request.user_account.organization)})


class ComplianceRulesApproveView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Settings"], summary="Approve and activate a pathway's rule set from a date (Administrator)", request=None, operation_id="compliance_rules_approve", responses=JSON_RESPONSE)
    def post(self, request):
        if not _is_admin(request):
            return Response({"detail": "Only an Administrator can approve compliance rules."}, status=status.HTTP_403_FORBIDDEN)
        try:
            effective_from = date.fromisoformat(str(request.data.get("effective_from") or ""))
        except ValueError:
            return Response({"detail": "Give the effective date as YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
        track = request.data.get("track")
        try:
            created = rules.approve(request.user_account.organization, request.user_account, track=track, effective_from=effective_from,
                                    overrides=request.data.get("overrides") or {}, note=request.data.get("note") or "")
        except ValidationError as exc:
            return _bad_request(exc)
        for rule_set in created:
            write_audit(action="compliance_rules.approve", entity_type="compliance_rule_set", entity_id=rule_set.id, request=request,
                        after={"track": track, "effective_from": rule_set.effective_from.isoformat(), "note": rule_set.approval_note})
        return Response({"can_approve": True, "tracks": rules.catalog(request.user_account.organization)}, status=status.HTTP_201_CREATED)


class OrganizationSettingsView(APIView):
    permission_classes = [HasWorkspace]

    def _payload(self, request):
        org = request.user_account.organization
        return {"name": org.name, "require_external_reporting": org.require_external_reporting, "time_zone": org.time_zone,
                "can_edit": _is_admin(request)}

    @extend_schema(tags=["Settings"], summary="Organization settings", operation_id="organization_settings", responses=JSON_RESPONSE)
    def get(self, request):
        return Response(self._payload(request))

    @extend_schema(tags=["Settings"], summary="Update organization settings (Administrator)", request=None, operation_id="organization_settings_update", responses=JSON_RESPONSE)
    def patch(self, request):
        if not _is_admin(request):
            return Response({"detail": "Only an Administrator can change organization settings."}, status=status.HTTP_403_FORBIDDEN)
        org = request.user_account.organization
        if "require_external_reporting" in request.data:
            before = org.require_external_reporting
            org.require_external_reporting = bool(request.data["require_external_reporting"])
            org.save(update_fields=["require_external_reporting", "updated_at"])
            write_audit(action="organization.settings", entity_type="organization", entity_id=org.id, request=request,
                        before={"require_external_reporting": before}, after={"require_external_reporting": org.require_external_reporting})
        if "time_zone" in request.data:
            zone = str(request.data["time_zone"] or "").strip()
            try:
                ZoneInfo(zone)
            except (ZoneInfoNotFoundError, ValueError):
                return Response({"detail": "Choose a valid time zone (for example America/New_York)."}, status=status.HTTP_400_BAD_REQUEST)
            before = org.time_zone
            org.time_zone = zone
            org.save(update_fields=["time_zone", "updated_at"])
            write_audit(action="organization.settings", entity_type="organization", entity_id=org.id, request=request,
                        before={"time_zone": before}, after={"time_zone": zone})
        return Response(self._payload(request))
