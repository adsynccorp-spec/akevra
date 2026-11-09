from drf_spectacular.utils import extend_schema
from rest_framework import status as http
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import invitations as inv
from apps.accounts.models import Invitation
from apps.core import rls
from apps.core.api_serializers import (
    JSON_RESPONSE,
    InvitationActivateSerializer,
    InvitationCreateSerializer,
    InvitationVerifySerializer,
)
from apps.core.exceptions import AccountLocked
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission


def _error(message, status):
    return Response({"detail": message}, status=status)


def _unavailable(exc):
    """410 with whatever details are safe to show on the 'no longer valid' screen."""
    body = {"detail": "This invitation is no longer valid.", "code": "invitation_unavailable"}
    if exc.invitation is not None:
        with rls.rls_bypass():
            body["invitation"] = inv.serialize_public(exc.invitation)
    return Response(body, status=http.HTTP_410_GONE)


def _locked(exc):
    return Response(
        {
            "detail": "Account is locked.",
            "locked_until": exc.locked_until.isoformat() if exc.locked_until else None,
        },
        status=423,
    )


# ---------------------------------------------------------------------------
# Invitee side — public, authenticated only by the invitation token
# ---------------------------------------------------------------------------
class PublicInvitationView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class InvitationDetailView(PublicInvitationView):
    @extend_schema(tags=["Invitations"], summary="Invitation details for the activation screen", responses=JSON_RESPONSE)
    def get(self, request, token):
        with rls.rls_bypass():
            invitation = inv.find_invitation(token)
            if invitation is None:
                return Response({"detail": "Invitation not found.", "code": "invitation_not_found"}, status=404)
            if invitation.state != "pending":
                return _unavailable(inv.InvitationUnavailable(invitation))
            return Response(inv.serialize_public(invitation))


class InvitationVerifyView(PublicInvitationView):
    @extend_schema(
        tags=["Invitations"],
        summary="Step 1: check name and password, get authenticator setup",
        request=InvitationVerifySerializer,
        responses=JSON_RESPONSE,
    )
    def post(self, request, token):
        try:
            result = inv.verify_invitation(token, request.data.get("full_name"), request.data.get("password"))
        except inv.InvitationUnavailable as exc:
            return _unavailable(exc)
        except inv.InvitationError as exc:
            return _error(str(exc), 400)
        except AccountLocked as exc:
            return _locked(exc)
        return Response(result)


class InvitationActivateView(PublicInvitationView):
    @extend_schema(
        tags=["Invitations"],
        summary="Step 2: confirm authenticator code and activate the account",
        request=InvitationActivateSerializer,
        responses=JSON_RESPONSE,
    )
    def post(self, request, token):
        try:
            result = inv.activate_invitation(
                token,
                request.data.get("full_name"),
                request.data.get("password"),
                request.data.get("code"),
            )
        except inv.InvitationUnavailable as exc:
            return _unavailable(exc)
        except (inv.InvitationError, inv.InvalidCode) as exc:
            return _error(str(exc), 400)
        except AccountLocked as exc:
            return _locked(exc)
        return Response(result, status=http.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Administrator side — RBAC-002: Administrator creates User Accounts
# ---------------------------------------------------------------------------
class IsUserManager(HasWorkspace):
    message = "Only an Administrator can manage invitations."

    def has_permission(self, request, view):
        return super().has_permission(request, view) and has_permission(
            request.user_account, "org.users.manage"
        )


class InvitationListView(APIView):
    permission_classes = [IsUserManager]

    @extend_schema(tags=["Invitations"], summary="List this organization's invitations", responses=JSON_RESPONSE)
    def get(self, request):
        invitations = Invitation.objects.filter(organization=request.organization).select_related("invited_by", "accepted_user_account")
        return Response([inv.serialize_invitation(i) for i in invitations])

    @extend_schema(
        tags=["Invitations"],
        summary="Invite a person to this organization",
        request=InvitationCreateSerializer,
        responses=JSON_RESPONSE,
    )
    def post(self, request):
        try:
            invitation, raw, delivery = inv.create_invitation(
                inviter=request.user_account,
                email=request.data.get("email"),
                credential_type=request.data.get("credential_type") or "",
                org_role=request.data.get("org_role") or "",
                request=request,
            )
        except inv.InvitationError as exc:
            return _error(str(exc), 400)
        return Response(
            {**inv.serialize_invitation(invitation, raw), **delivery},
            status=http.HTTP_201_CREATED,
        )


class InvitationActionView(APIView):
    permission_classes = [IsUserManager]
    action = None  # "resend" | "revoke", set in urls

    @extend_schema(tags=["Invitations"], summary="Resend or revoke a pending invitation", responses=JSON_RESPONSE)
    def post(self, request, pk):
        invitation = (
            Invitation.objects.filter(organization=request.organization, pk=pk)
            .select_related("invited_by")
            .first()
        )
        if invitation is None:
            return _error("Not found.", 404)
        try:
            if self.action == "resend":
                raw, delivery = inv.resend_invitation(invitation, request=request)
                return Response({**inv.serialize_invitation(invitation, raw), **delivery})
            inv.revoke_invitation(invitation, request=request)
        except inv.InvitationError as exc:
            return _error(str(exc), 400)
        return Response(inv.serialize_invitation(invitation))


class MemberCredentialView(APIView):
    """An Administrator records or corrects a member's credential (the source of truth for which
    pathways they may use). Existing relationships are not changed; each change is audited."""

    permission_classes = [IsUserManager]

    @extend_schema(tags=["Invitations"], summary="Set a member's credential", responses=JSON_RESPONSE)
    def post(self, request, pk):
        from apps.accounts.models import CredentialType, UserAccount
        from apps.audit.services import write_audit

        account = UserAccount.objects.filter(organization=request.organization, pk=pk).first()
        if account is None:
            return _error("Not found.", 404)
        credential = request.data.get("credential_type") or ""
        if credential not in ("", *CredentialType.values):
            return _error("Unknown credential type.", 400)
        before = account.credential_type
        if before != credential:
            account.credential_type = credential
            account.save(update_fields=["credential_type", "updated_at"])
            write_audit(action="user.credential_changed", entity_type="user_account", entity_id=account.id, request=request,
                        before={"credential_type": before}, after={"credential_type": credential})
        return Response({"id": str(account.id), "credential_type": account.credential_type})


class MemberDepartView(APIView):
    """An Administrator records that a person has left: their workspace sign-in ends and their active
    relationships are concluded. Nothing they recorded is deleted; authorized viewers keep access."""

    permission_classes = [IsUserManager]

    @extend_schema(tags=["Invitations"], summary="Record that a member has left the organization", responses=JSON_RESPONSE)
    def post(self, request, pk):
        from django.core.exceptions import ValidationError

        from apps.accounts.models import UserAccount
        from apps.audit.services import write_audit
        from apps.supervision import closure

        account = UserAccount.objects.filter(organization=request.organization, pk=pk).first()
        if account is None:
            return _error("Not found.", 404)
        try:
            concluded = closure.depart(account, request.user_account, notes=request.data.get("notes") or "")
        except ValidationError as exc:
            return _error(" ".join(exc.messages), 400)
        write_audit(action="user.departed", entity_type="user_account", entity_id=account.id, request=request,
                    after={"record_status": account.record_status, "concluded_relationships": [str(c.relationship_id) for c in concluded]})
        return Response({"id": str(account.id), "record_status": account.record_status, "concluded_relationships": len(concluded)})
