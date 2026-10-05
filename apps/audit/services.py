from apps.audit.models import AuditEvent


def write_audit(
    *,
    action,
    entity_type,
    entity_id,
    request=None,
    organization=None,
    before=None,
    after=None,
    metadata=None,
):
    identity = None
    user_account = None
    if request is not None:
        user = getattr(request, "user", None)
        if getattr(user, "is_authenticated", False):
            identity = user
        user_account = getattr(request, "user_account", None)
        organization = organization or getattr(request, "organization", None)
    return AuditEvent.objects.create(
        organization=organization,
        actor_identity=identity,
        actor_user_account=user_account,
        action=action,
        entity_type=entity_type,
        entity_id=str(entity_id),
        before=before,
        after=after,
        metadata=metadata or {},
    )
