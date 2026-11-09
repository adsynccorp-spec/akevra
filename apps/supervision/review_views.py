"""A5 review comments and A4 audit trail API."""

from django.core.exceptions import ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission, is_party
from apps.supervision import audit_trail, review
from apps.supervision.monthly_views import _bad_request, _denied, _not_found, _relationship


def _optional_relationship(value):
    """(relationship, error_response). A malformed or unknown id is a 404."""
    if not value:
        return None, None
    try:
        rel = _relationship(value)
    except (ValueError, ValidationError):
        rel = None
    return (rel, None) if rel else (None, _not_found())


def _target(request, data):
    """Resolve the comment target to (target_type, record, relationship), or an error response."""
    target_type = review.normalize_target_type(data.get("target_type"))
    target_id = data.get("target_id")
    if target_type is None:
        return None, _bad_request(ValidationError(f"Unknown target type. Use one of: {', '.join(review.TARGET_LABELS)}."))
    if not target_id:
        return None, _bad_request(ValidationError("target_id is required."))
    rel, error = _optional_relationship(data.get("relationship_id"))
    if error:
        return None, error
    try:
        obj, rel = review.resolve_target(target_type, target_id, relationship=rel)
    except LookupError:
        return None, _not_found()
    except ValidationError as exc:
        return None, _bad_request(exc)
    return (target_type, obj, rel), None


class ReviewCommentsView(APIView):
    """GET/POST /review-comments?target_type=&target_id=[&relationship_id=]. Comments are immutable:
    there is no PATCH, PUT or DELETE."""

    permission_classes = [HasWorkspace]
    http_method_names = ["get", "post", "head", "options"]

    @extend_schema(tags=["Review"], summary="Review comments on one record", operation_id="review_comments_list", responses=JSON_RESPONSE)
    def get(self, request):
        resolved, error = _target(request, request.query_params)
        if error:
            return error
        target_type, obj, rel = resolved
        denied = _denied(request, "relationship.view", rel, target_type, obj.id)
        if denied:
            return denied
        if review.hidden_from(request.user_account, target_type, obj, rel):
            return _not_found()
        me = request.user_account
        return Response({
            "target_type": target_type,
            "target_id": str(obj.id),
            "target_label": review.label_for(target_type, obj),
            "relationship_id": str(rel.id),
            "can_comment": is_party(me, rel),
            "comments": review.comments_for_target(rel, target_type, obj.id, me=me),
        })

    @extend_schema(tags=["Review"], summary="Add a review comment (immutable)", operation_id="review_comments_create", responses=JSON_RESPONSE)
    def post(self, request):
        data = {**request.query_params.dict(), **(request.data if hasattr(request.data, "get") else {})}
        resolved, error = _target(request, data)
        if error:
            return error
        target_type, obj, rel = resolved
        denied = _denied(request, "relationship.view", rel, target_type, obj.id)
        if denied:
            return denied
        if review.hidden_from(request.user_account, target_type, obj, rel):
            return _not_found()
        if not is_party(request.user_account, rel):
            write_audit(action="review_comment.create.denied", entity_type=target_type, entity_id=obj.id,
                        request=request, metadata={"relationship_id": str(rel.id)})
            return Response({"detail": "Only the Supervisor and Supervisee of this relationship can comment.",
                             "code": "review_comment_denied"}, status=status.HTTP_403_FORBIDDEN)
        try:
            comment = review.add_comment(request=request, relationship=rel, target_type=target_type,
                                         target=obj, body=data.get("body"))
        except ValidationError as exc:
            return _bad_request(exc)
        return Response(comment, status=status.HTTP_201_CREATED)


class RelationshipReviewCommentsView(APIView):
    """GET /relationships/<id>/review-comments: the relationship's whole feedback trail, oldest first."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Review"], summary="Feedback trail for a relationship", operation_id="relationship_review_comments", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        denied = _denied(request, "relationship.view", rel, "supervisory_relationship", rel.id)
        if denied:
            return denied
        target_type = review.normalize_target_type(request.query_params.get("target_type")) if request.query_params.get("target_type") else None
        me = request.user_account
        comments = review.comments_for_relationship(rel, me=me, target_type=target_type)
        # Comments on a draft the caller may not see stay hidden with the draft
        comments = _without_hidden_drafts(me, rel, comments)
        return Response({
            "relationship_id": str(rel.id),
            "can_comment": is_party(me, rel),
            "target_types": [{"key": key, "label": label} for key, label in review.TARGET_LABELS.items()],
            "comments": comments,
        })


def _without_hidden_drafts(me, rel, comments):
    from apps.supervision.models import Assignment, AssignmentStatus, SessionStatus, SupervisionSession

    hidden = set()
    if not has_permission(me, "assignment.create", relationship=rel):
        hidden |= {str(pk) for pk in Assignment.objects.filter(cycle__relationship=rel, status=AssignmentStatus.DRAFT).values_list("pk", flat=True)}
    if not has_permission(me, "session.document", relationship=rel):
        hidden |= {str(pk) for pk in SupervisionSession.objects.filter(cycle__relationship=rel, status=SessionStatus.DRAFT).values_list("pk", flat=True)}
    return [c for c in comments if c["target_id"] not in hidden]


# ---------------------------------------------------------------------------
# A4 audit trail
# ---------------------------------------------------------------------------

def _audit_denied(request, entity_type, entity_id, relationship):
    write_audit(action="audit.view.denied", entity_type=entity_type, entity_id=entity_id, request=request,
                metadata={"relationship_id": str(relationship.id)} if relationship else {})
    return Response({"detail": "Not permitted.", "code": "audit_view_denied"}, status=status.HTTP_403_FORBIDDEN)


class AuditTrailView(APIView):
    """GET /audit/trail?entity_type=&entity_id=: who did what, and when, to one record."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Audit trail for one record", operation_id="audit_trail", responses=JSON_RESPONSE)
    def get(self, request):
        entity_type = audit_trail.normalize_entity_type(request.query_params.get("entity_type"))
        entity_id = request.query_params.get("entity_id")
        if entity_type is None:
            return _bad_request(ValidationError("Unknown entity type. Use one of: " + ", ".join(audit_trail.RECORD_TYPES) + "."))
        if not entity_id:
            return _bad_request(ValidationError("entity_id is required."))
        resolved = audit_trail.resolve_record(entity_type, entity_id)
        if resolved is None:
            return _not_found()
        obj, rel, supervisee = resolved
        me = request.user_account
        # Someone who can't see the relationship at all learns nothing about the record
        if rel is not None and not has_permission(me, "relationship.view", relationship=rel) and not has_permission(me, "audit.view", relationship=rel):
            return _not_found()
        if not audit_trail.can_view(me, rel, supervisee):
            return _audit_denied(request, entity_type, obj.id, rel)
        show_values = audit_trail.can_view(me, rel, supervisee, permission="clinical.view")
        events = audit_trail.record_trail(request.organization, entity_type, obj.id, show_values=show_values, me=me)
        return Response({
            "entity_type": entity_type,
            "entity_type_label": audit_trail.type_label(entity_type),
            "entity_id": str(obj.id),
            "record_label": audit_trail.record_label(entity_type, obj),
            "relationship_id": str(rel.id) if rel else None,
            "values_visible": show_values,
            "events": events,
        })


class RelationshipAuditTrailView(APIView):
    """GET /relationships/<id>/audit-trail?entity_type=&actor=&page=&page_size=: every record of the
    relationship, newest first."""

    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Audit"], summary="Audit trail for a relationship", operation_id="relationship_audit_trail", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        me = request.user_account
        if not audit_trail.can_view(me, rel):
            return _audit_denied(request, "supervisory_relationship", rel.id, rel)
        raw_type = request.query_params.get("entity_type")
        entity_type = audit_trail.normalize_entity_type(raw_type) if raw_type else None
        if raw_type and entity_type is None:
            return _bad_request(ValidationError("Unknown entity type."))
        try:
            page = max(1, int(request.query_params.get("page", 1)))
            page_size = min(200, max(1, int(request.query_params.get("page_size", 50))))
        except ValueError:
            return _bad_request(ValidationError("page and page_size must be numbers."))
        show_values = has_permission(me, "clinical.view", relationship=rel)
        data = audit_trail.relationship_trail(rel, show_values=show_values, entity_type=entity_type,
                                              actor=request.query_params.get("actor") or None, me=me,
                                              page=page, page_size=page_size)
        data["relationship_id"] = str(rel.id)
        return Response(data)
