"""AI Teaching Assistant API (M7 Part B): request, list and disposition suggestions, with full provenance.

Who sees what (decided here, enforced in `_visible`):
* Viewing needs `idp.view` on the relationship; requesting and dispositioning need `idp.manage`
  (the relationship's Supervisor).
* The Supervisee of a relationship never sees pending, rejected or superseded suggestions: those are the
  Supervisor's working material, not part of the record. They do see suggestions that were adopted into
  their plan (Accepted / Edited and accepted), with the provenance and disposition history, because that
  content is now in their official development plan (B5).
* A Clinical Director (oversight, `idp.view` without being a party) sees everything, for audit.
"""

from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.services import write_audit
from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.models import RelationshipRole
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission, relationship_role_for
from apps.supervision import ai
from apps.supervision.models import AISuggestion, SuggestionDisposition
from apps.supervision.monthly_views import (
    _bad_request,
    _denied,
    _not_found,
    _relationship,
    _relationship_summary,
    _relationships_with,
)

TAGS = ["AI Teaching Assistant"]


def _sees_all(me, relationship):
    """Everything except a Supervisee looking at their own relationship."""
    return relationship_role_for(me, relationship) != RelationshipRole.SUPERVISEE


def _visible(me, qs, relationships):
    full = [rel.id for rel in relationships if _sees_all(me, rel)]
    return qs.filter(Q(relationship_id__in=full) | Q(disposition__in=ai.ADOPTED))


def _queryset():
    return AISuggestion.objects.select_related(
        "relationship", "relationship__supervisee", "requested_by", "dispositioned_by", "superseded_by",
    ).prefetch_related("events__actor", "adopted_goals__plan")


def _counts(items):
    pending = [s for s in items if s["disposition"] == SuggestionDisposition.PENDING]
    return {
        "awaiting_review": sum(1 for s in pending if not s["stale"]),
        "stale": sum(1 for s in pending if s["stale"]),
        "accepted": sum(1 for s in items if s["disposition"] == SuggestionDisposition.ACCEPTED),
        "edited_accepted": sum(1 for s in items if s["disposition"] == SuggestionDisposition.EDITED),
        "rejected": sum(1 for s in items if s["disposition"] == SuggestionDisposition.REJECTED),
        "superseded": sum(1 for s in items if s["disposition"] == SuggestionDisposition.SUPERSEDED),
        "total": len(items),
    }


def _serialize_all(suggestions):
    hashes = {}
    return [ai.serialize_suggestion(s, current_hashes=hashes) for s in suggestions]


class RelationshipSuggestionsView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Every AI suggestion for a relationship, including rejected and superseded",
                   operation_id="ai_suggestions_relationship", responses=JSON_RESPONSE)
    def get(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "idp.view", rel, "ai_suggestion", "list"):
            return denied
        me = request.user_account
        items = _serialize_all(_visible(me, _queryset().filter(relationship=rel), [rel]))
        return Response({
            "relationship": _relationship_summary(rel),
            "can_manage": has_permission(me, "idp.manage", relationship=rel),
            "generator": ai.generator_info(rel.organization),
            "counts": _counts(items),
            "suggestions": items,
        })


class GenerateSuggestionsView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Request a new batch of suggestions (supersedes earlier pending ones)",
                   description="Body (optional): {mode: teaching | justification}. Teaching mode (the default) poses a reasoning "
                               "question first and the Supervisor reveals the suggestion; justification mode shows it directly.",
                   operation_id="ai_suggestions_generate", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk):
        rel = _relationship(pk)
        if rel is None:
            return _not_found()
        if denied := _denied(request, "idp.manage", rel, "ai_suggestion", "generate"):
            return denied
        try:
            data = request.data if isinstance(request.data, dict) else {}
            created = ai.generate(rel, request.user_account, mode=data.get("mode") or "teaching", request=request)
        except ValidationError as exc:
            return _bad_request(exc)
        items = _serialize_all(_queryset().filter(pk__in=[s.pk for s in created]).order_by("created_at"))
        return Response({"generator": ai.generator_info(rel.organization), "suggestions": items}, status=status.HTTP_201_CREATED)


class AllSuggestionsView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="AI suggestions across my relationships (the AI Teaching Assistant page)",
                   operation_id="ai_suggestions_all", responses=JSON_RESPONSE)
    def get(self, request):
        me = request.user_account
        rels = _relationships_with(request, "idp.view")
        qs = _visible(me, _queryset().filter(relationship__in=rels), rels)
        if rel_id := request.query_params.get("relationship"):
            qs = qs.filter(relationship_id=rel_id)
        items = _serialize_all(qs)
        by_rel = {}
        for item in items:
            by_rel.setdefault(item["relationship_id"], []).append(item)
        return Response({
            "generator": ai.generator_info(me.organization),
            "counts": _counts(items),
            "relationships": [
                {**_relationship_summary(rel), "status": rel.status,
                 "can_manage": has_permission(me, "idp.manage", relationship=rel),
                 "counts": _counts(by_rel.get(str(rel.id), []))}
                for rel in rels
            ],
            "suggestions": items,
        })


def _load(request, pk):
    """(suggestion, None) or (None, error response). A suggestion the caller may not see is a 404."""
    suggestion = _queryset().filter(pk=pk).first()
    if suggestion is None:
        return None, _not_found()
    if denied := _denied(request, "idp.view", suggestion.relationship, "ai_suggestion", suggestion.id):
        return None, denied
    me = request.user_account
    if not _sees_all(me, suggestion.relationship) and suggestion.disposition not in ai.ADOPTED:
        return None, _not_found()
    return suggestion, None


class SuggestionDetailView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="One AI suggestion with full provenance and disposition history",
                   operation_id="ai_suggestions_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        suggestion, error = _load(request, pk)
        if error:
            return error
        data = ai.serialize_suggestion(suggestion)
        data["can_manage"] = has_permission(request.user_account, "idp.manage", relationship=suggestion.relationship)
        return Response(data)


class SuggestionDispositionView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Accept, Edit & Accept, or Reject a suggestion",
                   description="Body: {action: accept | edit_accept | reject, final_content?: {category, title, description}, note?}. "
                               "Reject requires a note (the reason). Accepting adds the goal to a new development plan version.",
                   operation_id="ai_suggestions_disposition", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk):
        suggestion, error = _load(request, pk)
        if error:
            return error
        if denied := _denied(request, "idp.manage", suggestion.relationship, "ai_suggestion", suggestion.id):
            return denied
        data = request.data if isinstance(request.data, dict) else {}
        try:
            ai.disposition(
                suggestion, request.user_account, data.get("action"),
                final_content=data.get("final_content"), note=data.get("note") or "", request=request,
            )
        except PermissionDenied:
            write_audit(action="idp.manage.denied", entity_type="ai_suggestion", entity_id=suggestion.id, request=request)
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        except ai.StaleSuggestion as exc:
            return Response({"detail": exc.messages[0], "code": "suggestion_stale"}, status=status.HTTP_409_CONFLICT)
        except ValidationError as exc:
            return _bad_request(exc)
        fresh = _queryset().get(pk=suggestion.pk)
        out = ai.serialize_suggestion(fresh)
        out["can_manage"] = True
        return Response(out)


class SuggestionRevealView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Teaching mode: reveal a suggestion (optionally recording the Supervisor's own reasoning first)",
                   description="Body (optional): {reflection}.", operation_id="ai_suggestions_reveal", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk):
        suggestion, error = _load(request, pk)
        if error:
            return error
        if denied := _denied(request, "idp.manage", suggestion.relationship, "ai_suggestion", suggestion.id):
            return denied
        data = request.data if isinstance(request.data, dict) else {}
        try:
            ai.reveal(suggestion, request.user_account, reflection=data.get("reflection") or "", request=request)
        except PermissionDenied:
            return Response({"detail": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
        except ValidationError as exc:
            return _bad_request(exc)
        out = ai.serialize_suggestion(_queryset().get(pk=suggestion.pk))
        out["can_manage"] = True
        return Response(out)
