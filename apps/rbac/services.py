from apps.rbac.matrix import MATRIX, RBAC_003_RIGHTS
from apps.rbac.models import OrgRole, OrganizationRoleGrant, RelationshipRole


def org_roles_for(user_account):
    """Active org roles, cached on the account instance. request.user_account is loaded
    fresh per request, so the cache is request-scoped; without it a list view ran one
    grant query per relationship per permission (hundreds of round trips to the database)."""
    if user_account is None:
        return set()
    cached = getattr(user_account, "_org_roles_cache", None)
    if cached is None:
        cached = frozenset(
            OrganizationRoleGrant.objects.filter(
                user_account=user_account,
                record_status="active",
            ).values_list("role", flat=True)
        )
        user_account._org_roles_cache = cached
    return set(cached)


def relationship_role_for(user_account, relationship):
    if user_account is None or relationship is None:
        return None
    if relationship.supervisor_id == user_account.id:
        return RelationshipRole.SUPERVISOR
    if relationship.supervisee_id == user_account.id:
        return RelationshipRole.SUPERVISEE
    return None


def is_party(user_account, relationship):
    return relationship_role_for(user_account, relationship) is not None


def has_permission(user_account, permission, relationship=None):
    """Evaluate a permission for this user, optionally in a relationship context.

    DEC-057: Supervisor grants never leak into a relationship where the same
    user is the Supervisee, and vice versa.
    """
    if user_account is None:
        return False

    roles = org_roles_for(user_account)
    rel_role = relationship_role_for(user_account, relationship) if relationship else None

    # Relationship-scoped clinical role (DEC-057) — checked first so that a
    # user who is Supervisee on THIS relationship cannot use a Supervisor
    # grant from a DIFFERENT relationship.
    if rel_role is not None:
        allowed = MATRIX[rel_role].get(permission, False)
        if allowed is True:
            return True

    # Clinical Director: oversight across the organization (not relationship-bound).
    if OrgRole.CLINICAL_DIRECTOR in roles:
        allowed = MATRIX[OrgRole.CLINICAL_DIRECTOR].get(permission, False)
        if allowed is True:
            return True

    # Administrator: org settings plus the narrow RBAC-003 self-party exception.
    if OrgRole.ADMINISTRATOR in roles:
        allowed = MATRIX[OrgRole.ADMINISTRATOR].get(permission, False)
        if allowed is True:
            return True
        if allowed == "self-party":
            if permission not in RBAC_003_RIGHTS:
                return False
            if relationship is None:
                # create with no object yet — caller must pass the would-be
                # relationship (parties already assigned) or use can_create_relationship.
                return False
            return is_party(user_account, relationship)

    return False


def can_create_relationship(user_account, supervisor_id, supervisee_id):
    if user_account is None:
        return False
    roles = org_roles_for(user_account)
    # A BCBA acting as Supervisor may create a relationship they will supervise.
    if str(user_account.id) == str(supervisor_id):
        if user_account.credential_type == "bcba":
            return True
    # M3: an Administrator may facilitate any eligible relationship in their organization
    # (the view still enforces a BCBA Supervisor and same-organization parties).
    return OrgRole.ADMINISTRATOR in roles


def landing_views(user_account):
    """Minimal walking-skeleton landings this user should see."""
    views = []
    roles = org_roles_for(user_account)
    if OrgRole.ADMINISTRATOR in roles:
        views.append("administrator")
    if OrgRole.CLINICAL_DIRECTOR in roles:
        views.append("clinical_director")

    from apps.supervision.models import SupervisoryRelationship

    supervised = SupervisoryRelationship.objects.filter(
        supervisor=user_account, record_status="active"
    ).exists()
    as_supervisee = SupervisoryRelationship.objects.filter(
        supervisee=user_account, record_status="active"
    ).exists()
    if supervised:
        views.append("supervisor")
    if as_supervisee:
        views.append("supervisee")
    return views
