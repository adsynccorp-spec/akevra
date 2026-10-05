from django.contrib.auth.hashers import check_password

from apps.accounts.models import AuthSession, CredentialType, Identity, UserAccount
from apps.accounts.mfa import decrypt_mfa_secret, verify_totp
from apps.audit.services import write_audit
from apps.core import rls
from apps.core.exceptions import AccountLocked, AuthenticationFailed
from apps.rbac.models import OrgRole
from apps.rbac.services import landing_views, org_roles_for


def _dummy_password_check():
    check_password("invalid", "pbkdf2_sha256$1$invalid$invalid")


def authenticate_identity(email, password):
    email = (email or "").strip().lower()
    with rls.rls_bypass():
        identity = Identity.objects.filter(email=email).first()
    if identity is None:
        _dummy_password_check()
        raise AuthenticationFailed("Invalid email or password.")
    rls.set_tenant_context(identity_id=identity.id, workspace_pending=True)
    if identity.is_locked:
        raise AccountLocked(identity.locked_until)
    if not identity.check_password(password) or not identity.is_active:
        identity.register_failed_login()
        write_audit(
            action="login.failed",
            entity_type="login_identity",
            entity_id=identity.id,
            metadata={"email": email},
        )
        if identity.is_locked:
            raise AccountLocked(identity.locked_until)
        raise AuthenticationFailed("Invalid email or password.")
    identity.reset_failed_logins()
    return identity


def accounts_for(identity):
    rls.set_tenant_context(identity_id=identity.id, workspace_pending=True)
    return list(
        UserAccount.objects.filter(identity=identity, record_status="active").select_related(
            "organization"
        )
    )


def serialize_org(organization):
    return {"id": str(organization.id), "name": organization.name, "slug": organization.slug}


def serialize_user_account(account):
    return {
        "id": str(account.id),
        "email": account.email,
        "first_name": account.first_name,
        "last_name": account.last_name,
        "full_name": account.full_name,
        "credential_type": account.credential_type,
        "organization": serialize_org(account.organization),
        "org_roles": sorted(org_roles_for(account)),
        "landing_views": landing_views(account),
    }


def serialize_workspace(account):
    """One entry on the Select Workspace screen (REQ-094 / UI-014).

    Only the caller's own UserAccounts are ever passed in, so the narrow auth-bootstrap
    bypass here cannot expose another person's data.
    """
    from django.db.models import Max, Q

    from apps.supervision.models import SupervisoryRelationship

    with rls.rls_bypass():
        roles = sorted(org_roles_for(account))
        relationships = SupervisoryRelationship.objects.filter(
            Q(supervisor=account) | Q(supervisee=account), status="active"
        )
        supervises = relationships.filter(supervisor=account).exists()
        last_accessed = AuthSession.objects.filter(user_account=account).aggregate(
            last=Max("last_seen_at")
        )["last"]
        active_count = relationships.count()

    if roles:
        role_label = OrgRole(roles[0]).label
    elif active_count:
        role_label = "Supervisor" if supervises else "Supervisee"
    else:
        role_label = CredentialType(account.credential_type).label if account.credential_type else "Member"

    return {
        **serialize_org(account.organization),
        "role_label": role_label,
        "credential_type": account.credential_type,
        "active_relationships": active_count,
        "last_accessed_at": last_accessed.isoformat() if last_accessed else None,
    }


def workspace_payload(session, accounts):
    return {
        "status": "workspace_required",
        "email": session.identity.email,
        "organizations": [serialize_workspace(a) for a in accounts],
    }


def _token(session):
    return getattr(session, "raw_token", None)


def complete_post_mfa(session, accounts=None):
    if accounts is None:
        accounts = accounts_for(session.identity)
    session.mfa_verified = True
    session.save(update_fields=["mfa_verified", "updated_at"])

    if len(accounts) == 1:
        rls.set_tenant_context(
            organization_id=accounts[0].organization_id,
            identity_id=session.identity_id,
            user_account_id=accounts[0].id,
        )
        session.bind_workspace(accounts[0])
        write_audit(
            action="workspace.auto_selected",
            entity_type="organization",
            entity_id=accounts[0].organization_id,
            organization=accounts[0].organization,
        )
        payload = {
            "status": "authenticated",
            "user": serialize_user_account(accounts[0]),
        }
        token = _token(session)
        if token:
            payload["token"] = token
        return payload

    payload = workspace_payload(session, accounts)
    token = _token(session)
    if token:
        payload["token"] = token
    return payload


def login(email, password):
    identity = authenticate_identity(email, password)
    accounts = accounts_for(identity)
    if not accounts:
        raise AuthenticationFailed("No organization membership for this credential.")

    session = AuthSession.issue(identity, mfa_verified=not identity.mfa_enabled)
    write_audit(
        action="login.success",
        entity_type="login_identity",
        entity_id=identity.id,
        metadata={"mfa_enabled": identity.mfa_enabled, "account_count": len(accounts)},
    )

    if identity.mfa_enabled:
        return {"status": "mfa_required", "token": session.raw_token}

    return complete_post_mfa(session, accounts)


def verify_mfa(session, code):
    identity = session.identity
    if not identity.mfa_enabled or not identity.mfa_secret_encrypted:
        raise AuthenticationFailed("MFA is not enabled.")
    secret = decrypt_mfa_secret(identity.mfa_secret_encrypted)
    if not verify_totp(secret, code or ""):
        identity.register_failed_login()
        write_audit(
            action="mfa.failed",
            entity_type="login_identity",
            entity_id=identity.id,
        )
        if identity.is_locked:
            session.revoke()
            raise AccountLocked(identity.locked_until)
        raise AuthenticationFailed("Invalid authentication code.")
    identity.reset_failed_logins()
    write_audit(action="mfa.verified", entity_type="login_identity", entity_id=identity.id)
    return complete_post_mfa(session)


def select_workspace(session, organization_id):
    accounts = accounts_for(session.identity)
    match = next((a for a in accounts if str(a.organization_id) == str(organization_id)), None)
    if match is None:
        raise AuthenticationFailed("That organization is not available for this credential.")
    rls.set_tenant_context(
        organization_id=match.organization_id,
        identity_id=session.identity_id,
        user_account_id=match.id,
    )
    session.bind_workspace(match)
    write_audit(
        action="workspace.selected",
        entity_type="organization",
        entity_id=match.organization_id,
        organization=match.organization,
    )
    return {"status": "authenticated", "user": serialize_user_account(match)}
