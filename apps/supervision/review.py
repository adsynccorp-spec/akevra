"""A5 review comments: one feedback trail per relationship.

A comment is attached to one record (an assignment, a fieldwork hours entry, a service hours
record, a monthly cycle, a development plan milestone or a supervision session) and always
belongs to the relationship that record sits in. Comments are immutable: there is no edit and
no delete; a correction is a new comment.

`comments_for_relationship(relationship)` is the export helper: the whole trail, oldest first,
each comment serialized with its target label.
"""

from django.core.exceptions import ValidationError

from apps.audit.services import write_audit
from apps.rbac.services import has_permission, relationship_role_for
from apps.supervision.models import (
    Assignment,
    AssignmentStatus,
    DevelopmentPlanMilestone,
    HoursEntry,
    MonthlyCycle,
    ReviewComment,
    ReviewTarget,
    ServiceHoursRecord,
    SessionStatus,
    SupervisionSession,
)

MAX_BODY = 5000

# Short names the frontend may use for a target type
TARGET_ALIASES = {
    "assignment": ReviewTarget.ASSIGNMENT,
    "hours": ReviewTarget.HOURS_ENTRY,
    "hours_entry": ReviewTarget.HOURS_ENTRY,
    "service_hours": ReviewTarget.SERVICE_HOURS,
    "service_hours_record": ReviewTarget.SERVICE_HOURS,
    "cycle": ReviewTarget.MONTHLY_CYCLE,
    "monthly_cycle": ReviewTarget.MONTHLY_CYCLE,
    "milestone": ReviewTarget.MILESTONE,
    "development_plan_milestone": ReviewTarget.MILESTONE,
    "session": ReviewTarget.SESSION,
    "supervision_session": ReviewTarget.SESSION,
}

TARGET_LABELS = dict(ReviewTarget.choices)


def normalize_target_type(value):
    return TARGET_ALIASES.get((value or "").strip().lower())


def _load(target_type, target_id):
    """The target record (RLS limits it to the caller's workspace), or None."""
    if target_type == ReviewTarget.ASSIGNMENT:
        return Assignment.objects.select_related("cycle__relationship__supervisor", "cycle__relationship__supervisee").filter(pk=target_id).first()
    if target_type == ReviewTarget.HOURS_ENTRY:
        return HoursEntry.objects.select_related("cycle__relationship__supervisor", "cycle__relationship__supervisee").filter(pk=target_id).first()
    if target_type == ReviewTarget.SESSION:
        return SupervisionSession.objects.select_related("cycle__relationship__supervisor", "cycle__relationship__supervisee").filter(pk=target_id).first()
    if target_type == ReviewTarget.MONTHLY_CYCLE:
        return MonthlyCycle.objects.select_related("relationship__supervisor", "relationship__supervisee").filter(pk=target_id).first()
    if target_type == ReviewTarget.MILESTONE:
        return DevelopmentPlanMilestone.objects.select_related("plan__relationship__supervisor", "plan__relationship__supervisee").filter(pk=target_id).first()
    if target_type == ReviewTarget.SERVICE_HOURS:
        return ServiceHoursRecord.objects.select_related("supervisee").filter(pk=target_id).first()
    return None


def relationship_of(target_type, obj):
    """The relationship a target belongs to; None for a service hours record (supervisee-owned)."""
    if target_type in (ReviewTarget.ASSIGNMENT, ReviewTarget.HOURS_ENTRY, ReviewTarget.SESSION):
        return obj.cycle.relationship
    if target_type == ReviewTarget.MONTHLY_CYCLE:
        return obj.relationship
    if target_type == ReviewTarget.MILESTONE:
        return obj.plan.relationship
    return None


def resolve_target(target_type, target_id, relationship=None):
    """(record, relationship) for a comment target, or raise LookupError (404) / ValidationError (400).

    A service hours record belongs to the supervisee, not to one relationship, so the caller names
    the relationship and it must be one of that supervisee's."""
    if target_type is None:
        raise ValidationError(f"Unknown target type. Use one of: {', '.join(ReviewTarget.values)}.")
    try:
        obj = _load(target_type, target_id)
    except (ValueError, ValidationError):
        obj = None
    if obj is None:
        raise LookupError
    if target_type == ReviewTarget.SERVICE_HOURS:
        if relationship is None:
            raise ValidationError("relationship_id is required for a service hours record.")
        if relationship.supervisee_id != obj.supervisee_id:
            raise ValidationError("This service hours record belongs to a different supervisee.")
        return obj, relationship
    owner = relationship_of(target_type, obj)
    if relationship is not None and relationship.id != owner.id:
        raise ValidationError("This record belongs to a different relationship.")
    return obj, owner


def hidden_from(user_account, target_type, obj, relationship):
    """Drafts stay with their author, as in the monthly work views."""
    if target_type == ReviewTarget.ASSIGNMENT and obj.status == AssignmentStatus.DRAFT:
        return not has_permission(user_account, "assignment.create", relationship=relationship)
    if target_type == ReviewTarget.SESSION and obj.status == SessionStatus.DRAFT:
        return not has_permission(user_account, "session.document", relationship=relationship)
    return False


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

def _month(year, month):
    from datetime import date

    return date(year, month, 1).strftime("%b %Y")


def _hours(value):
    return f"{float(value):g}"


def label_for(target_type, obj):
    if obj is None:
        return f"{TARGET_LABELS.get(target_type, 'Record')} (no longer available)"
    if target_type == ReviewTarget.ASSIGNMENT:
        return obj.title
    if target_type == ReviewTarget.HOURS_ENTRY:
        return f"Hours · {obj.occurred_on:%b %d, %Y} ({_hours(obj.hours)} h)".replace(" 0", " ")
    if target_type == ReviewTarget.SERVICE_HOURS:
        source = f" · {obj.external_organization}" if obj.external_organization else ""
        return f"Service hours · {_month(obj.year, obj.month)}{source}"
    if target_type == ReviewTarget.MONTHLY_CYCLE:
        return f"{_month(obj.year, obj.month)} cycle"
    if target_type == ReviewTarget.MILESTONE:
        return obj.title
    if target_type == ReviewTarget.SESSION:
        return f"Session · {obj.occurred_on:%b %d, %Y}".replace(" 0", " ")
    return TARGET_LABELS.get(target_type, "Record")


_MODELS = {
    ReviewTarget.ASSIGNMENT: Assignment,
    ReviewTarget.HOURS_ENTRY: HoursEntry,
    ReviewTarget.SERVICE_HOURS: ServiceHoursRecord,
    ReviewTarget.MONTHLY_CYCLE: MonthlyCycle,
    ReviewTarget.MILESTONE: DevelopmentPlanMilestone,
    ReviewTarget.SESSION: SupervisionSession,
}


def _labels(comments):
    """{(target_type, target_id): label} with one query per target type."""
    wanted = {}
    for c in comments:
        wanted.setdefault(c.target_entity_type, set()).add(c.target_entity_id)
    labels = {}
    for target_type, ids in wanted.items():
        model = _MODELS.get(target_type)
        found = {obj.id: obj for obj in model.objects.filter(pk__in=ids)} if model else {}
        for pk in ids:
            labels[(target_type, pk)] = label_for(target_type, found.get(pk))
    return labels


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _role(relationship, author):
    role = relationship_role_for(author, relationship)
    return {"supervisor": "Supervisor", "supervisee": "Supervisee"}.get(role, "") if role else ""


def serialize_comment(comment, *, label=None, me=None):
    relationship = comment.relationship
    return {
        "id": str(comment.id),
        "relationship_id": str(comment.relationship_id),
        "target_type": comment.target_entity_type,
        "target_type_label": TARGET_LABELS.get(comment.target_entity_type, comment.target_entity_type),
        "target_id": str(comment.target_entity_id),
        "target_label": label or "",
        "body": comment.body,
        "author": {"id": str(comment.author_id), "name": comment.author.full_name},
        "author_role": _role(relationship, comment.author),
        "created_at": comment.created_at.isoformat(),
        "is_mine": bool(me is not None and comment.author_id == me.id),
    }


def _queryset(relationship):
    return (
        ReviewComment.objects.filter(relationship=relationship, record_status="active")
        .select_related("author", "relationship")
        .order_by("created_at")
    )


def comments_for_relationship(relationship, *, me=None, target_type=None):
    """Export helper: every review comment on the relationship, oldest first.

    Returns a list of dicts:
      {id, relationship_id, target_type, target_type_label, target_id, target_label, body,
       author: {id, name}, author_role ("Supervisor" | "Supervisee" | ""), created_at (ISO 8601), is_mine}
    """
    comments = list(_queryset(relationship).filter(**({"target_entity_type": target_type} if target_type else {})))
    labels = _labels(comments)
    return [serialize_comment(c, label=labels.get((c.target_entity_type, c.target_entity_id)), me=me) for c in comments]


def comments_for_target(relationship, target_type, target_id, *, me=None):
    comments = list(_queryset(relationship).filter(target_entity_type=target_type, target_entity_id=target_id))
    labels = _labels(comments)
    return [serialize_comment(c, label=labels.get((c.target_entity_type, c.target_entity_id)), me=me) for c in comments]


def add_comment(*, request, relationship, target_type, target, body):
    """Record one immutable comment, audit it and tell the other party."""
    body = (body or "").strip()
    if not body:
        raise ValidationError("Write a comment before posting.")
    if len(body) > MAX_BODY:
        raise ValidationError(f"Keep a comment under {MAX_BODY} characters.")
    me = request.user_account
    comment = ReviewComment.objects.create(
        organization=relationship.organization,
        relationship=relationship,
        author=me,
        body=body,
        target_entity_type=target_type,
        target_entity_id=target.id,
    )
    label = label_for(target_type, target)
    write_audit(
        action="review_comment.create",
        entity_type="review_comment",
        entity_id=comment.id,
        request=request,
        after={"body": body, "target_entity_type": target_type, "target_entity_id": str(target.id)},
        metadata={"relationship_id": str(relationship.id), "target_type": target_type,
                  "target_id": str(target.id), "target_label": label},
    )
    _notify(relationship, me, label, body)
    return serialize_comment(comment, label=label, me=me)


def _notify(relationship, author, label, body):
    from apps.supervision.notifications import notify

    other = relationship.supervisee if author.id == relationship.supervisor_id else relationship.supervisor
    if other is None or other.id == author.id:
        return
    preview = body if len(body) <= 200 else body[:197] + "…"
    notify(other, "review_comment", f"{author.full_name} commented on {label}", body=preview,
           relationship=relationship, tab="feedback")
