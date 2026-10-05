from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed, PermissionDenied

from apps.accounts.models import AuthSession
from apps.core import rls
from apps.core.models import RecordStatus

_MFA_EXEMPT_PATHS = frozenset([
    "/api/v1/auth/login",
    "/api/v1/auth/mfa/verify",
    "/api/v1/auth/logout",
    "/api/v1/health",
    "/api/schema/",
    "/api/docs/",
])


class SessionTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith("Bearer "):
            return None
        raw = header[7:].strip()
        if not raw:
            return None

        with rls.rls_bypass():
            session = (
                AuthSession.objects.select_related("identity", "user_account", "organization")
                .filter(token_hash=AuthSession.hash_token(raw))
                .first()
            )
        if session is None or session.is_revoked:
            raise AuthenticationFailed("Invalid session.")
        if session.absolute_expired() or session.idle_expired():
            with rls.rls_bypass():
                session.revoke()
            raise AuthenticationFailed("Session expired.")

        # Issue 7 & 8: block and revoke if identity disabled
        if not session.identity.is_active:
            with rls.rls_bypass():
                session.revoke()
            raise AuthenticationFailed("Account is disabled.")

        # Issue 7 & 8: block and revoke if user_account disabled/archived
        # Check both status and record_status — record_status is the authoritative
        # archival field from SoftArchiveModel; status is an additional access-control field.
        if session.user_account_id:
            ua = session.user_account
            if ua.status != RecordStatus.ACTIVE or ua.record_status != RecordStatus.ACTIVE:
                with rls.rls_bypass():
                    session.revoke()
                raise AuthenticationFailed("Account is disabled.")

        with rls.rls_bypass():
            session.touch()
        request.auth_session = session

        if session.workspace_selected and session.user_account_id:
            rls.set_tenant_context(
                organization_id=session.organization_id,
                identity_id=session.identity_id,
                user_account_id=session.user_account_id,
            )
            request.organization = session.organization
            request.user_account = session.user_account
        else:
            rls.set_tenant_context(
                identity_id=session.identity_id,
                workspace_pending=True,
            )

        # Issue 6: enforce MFA centrally — exempt auth bootstrap paths
        path = request.path.rstrip("/")
        if not any(path.startswith(e.rstrip("/")) for e in _MFA_EXEMPT_PATHS):
            if not session.mfa_verified:
                raise PermissionDenied({"detail": "MFA verification required.", "status": "mfa_required"})

        return (session.identity, session)

    def authenticate_header(self, request):
        return "Bearer"


from apps.accounts.schema import SessionTokenScheme  # noqa: E402,F401
