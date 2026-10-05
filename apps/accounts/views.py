from django.utils import timezone
from drf_spectacular.utils import extend_schema, OpenApiExample
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.mfa import (
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    new_totp_secret,
    totp_uri,
    verify_totp,
)
from apps.accounts.services import login as do_login
from apps.accounts.services import select_workspace, serialize_user_account, verify_mfa
from apps.audit.services import write_audit
from apps.core.api_serializers import (
    JSON_RESPONSE,
    LoginSerializer,
    MFACodeSerializer,
    WorkspaceSelectSerializer,
)
from apps.core.exceptions import AccountLocked, AuthenticationFailed
from apps.supervision.models import SupervisoryRelationship


def _error(message, status=401, extra=None):
    body = {"detail": message}
    if extra:
        body.update(extra)
    return Response(body, status=status)


class LoginView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    @extend_schema(
        tags=["Auth"],
        summary="Sign in",
        request=LoginSerializer,
        examples=[
            OpenApiExample(
                "One organization (no workspace step)",
                value={"email": "alex@akevra.test", "password": "Sprint0!Akevra"},
            ),
            OpenApiExample(
                "Two organizations (workspace step)",
                value={"email": "admin@akevra.test", "password": "Sprint0!Akevra"},
            ),
            OpenApiExample(
                "MFA user",
                value={"email": "mfa@akevra.test", "password": "Sprint0!Akevra"},
            ),
        ],
        responses=JSON_RESPONSE,
    )
    def post(self, request):
        try:
            result = do_login(request.data.get("email"), request.data.get("password"))
        except AccountLocked as exc:
            return _error(
                "Account is locked.",
                status=423,
                extra={"locked_until": exc.locked_until.isoformat() if exc.locked_until else None},
            )
        except AuthenticationFailed as exc:
            return _error(str(exc))
        return Response(result)


class MFAVerifyView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Verify MFA code", request=MFACodeSerializer, responses=JSON_RESPONSE)
    def post(self, request):
        session = request.auth_session
        if session is None or session.mfa_verified:
            return _error("MFA is not pending for this session.")
        try:
            result = verify_mfa(session, request.data.get("code"))
        except AccountLocked as exc:
            return _error(
                "Account is locked.",
                status=423,
                extra={"locked_until": exc.locked_until.isoformat() if exc.locked_until else None},
            )
        except AuthenticationFailed as exc:
            return _error(str(exc))
        return Response(result)


class WorkspaceSelectView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Choose an organization", request=WorkspaceSelectSerializer, responses=JSON_RESPONSE)
    def post(self, request):
        session = request.auth_session
        if session is None or not session.mfa_verified:
            return _error("Complete authentication before selecting a workspace.")
        if session.workspace_selected:
            return _error("Workspace is already selected.", status=400)
        try:
            result = select_workspace(session, request.data.get("organization_id"))
        except AuthenticationFailed as exc:
            return _error(str(exc), status=403)
        return Response(result)


@extend_schema(tags=["Auth"], responses=JSON_RESPONSE)
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Sign out", responses=JSON_RESPONSE)
    def post(self, request):
        request.auth_session.revoke()
        write_audit(
            action="logout",
            entity_type="auth_session",
            entity_id=request.auth_session.id,
            request=request,
        )
        return Response({"status": "logged_out"})


class SessionView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Session status (idle timeout remaining)", responses=JSON_RESPONSE)
    def get(self, request):
        session = request.auth_session
        remaining_idle = None
        from django.conf import settings
        from datetime import timedelta

        idle_limit = timedelta(seconds=settings.AUTH_SESSION_IDLE_SECONDS)
        remaining_idle = (session.last_seen_at + idle_limit - timezone.now()).total_seconds()
        return Response({
            "mfa_verified": session.mfa_verified,
            "workspace_selected": session.workspace_selected,
            "idle_seconds_remaining": max(0, int(remaining_idle)),
            "absolute_expires_at": session.absolute_expires_at.isoformat(),
            "locked": session.identity.is_locked,
        })


@extend_schema(tags=["Auth"], responses=JSON_RESPONSE)
class MFAEnrollView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Start MFA enrollment", responses=JSON_RESPONSE)
    def post(self, request):
        if not request.user_account:
            return _error("Select a workspace first.", status=403)
        identity = request.user
        secret = new_totp_secret()
        identity.mfa_secret_encrypted = encrypt_mfa_secret(secret)
        identity.mfa_enabled = False
        identity.save(update_fields=["mfa_secret_encrypted", "mfa_enabled", "updated_at"])
        return Response({
            "secret": secret,
            "otpauth_uri": totp_uri(identity.email, secret),
            "issuer": request.user.email,
        })

    @extend_schema(tags=["Auth"], summary="Confirm MFA enrollment", request=MFACodeSerializer, responses=JSON_RESPONSE)
    def put(self, request):
        identity = request.user
        if not identity.mfa_secret_encrypted:
            return _error("Start enrollment first.", status=400)
        secret = decrypt_mfa_secret(identity.mfa_secret_encrypted)
        if not verify_totp(secret, request.data.get("code") or ""):
            return _error("Invalid authentication code.")
        identity.mfa_enabled = True
        identity.save(update_fields=["mfa_enabled", "updated_at"])
        write_audit(
            action="mfa.enabled",
            entity_type="login_identity",
            entity_id=identity.id,
            request=request,
        )
        return Response({"mfa_enabled": True})


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(tags=["Auth"], summary="Current user, roles, and relationships", responses=JSON_RESPONSE)
    def get(self, request):
        session = request.auth_session
        if not session.mfa_verified:
            return Response({"status": "mfa_required"})
        if not session.workspace_selected or not request.user_account:
            from apps.accounts.services import accounts_for, workspace_payload
            return Response(workspace_payload(session, accounts_for(session.identity)))
        account = request.user_account
        relationships = []
        for rel in SupervisoryRelationship.objects.select_related("supervisee").filter(supervisor=account):
            relationships.append({
                "id": str(rel.id),
                "role": "supervisor",
                "other_party": rel.supervisee.full_name,
                "track": rel.supervision_track,
            })
        for rel in SupervisoryRelationship.objects.select_related("supervisor").filter(supervisee=account):
            relationships.append({
                "id": str(rel.id),
                "role": "supervisee",
                "other_party": rel.supervisor.full_name,
                "track": rel.supervision_track,
            })
        data = serialize_user_account(account)
        data["status"] = "authenticated"
        data["relationships"] = relationships
        data["mfa_enabled"] = request.user.mfa_enabled
        return Response(data)
