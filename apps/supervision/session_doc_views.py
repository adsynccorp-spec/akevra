"""API for guided, AI-assisted session documentation (see apps.supervision.session_docs for the rules)."""

from django.core.exceptions import PermissionDenied, ValidationError
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.rbac.services import has_permission
from apps.supervision import session_docs
from apps.supervision.models import SessionStatus
from apps.supervision.monthly_views import _bad_request, _denied, _not_found, _session

TAGS = ["Session Documentation"]


def _load(request, pk):
    """(session, None) or (None, error). Needs session.view; a draft session exists only for its Supervisor."""
    session = _session(pk)
    if session is None:
        return None, _not_found()
    rel = session.cycle.relationship
    if denied := _denied(request, "session.view", rel, "supervision_session", session.id):
        return None, denied
    if session.status == SessionStatus.DRAFT and not has_permission(request.user_account, "session.document", relationship=rel):
        return None, _not_found()
    return session, None


def _run(request, session, fn):
    try:
        fn()
    except PermissionDenied as exc:
        return Response({"detail": str(exc) or "Not permitted."}, status=status.HTTP_403_FORBIDDEN)
    except ValidationError as exc:
        return _bad_request(exc)
    return Response(session_docs.serialize(_session(session.id), request.user_account))


class SessionDocumentationView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="A session's guided documentation, as the caller may see it",
                   operation_id="session_documentation_retrieve", responses=JSON_RESPONSE)
    def get(self, request, pk):
        session, error = _load(request, pk)
        return error or Response(session_docs.serialize(session, request.user_account))

    @extend_schema(tags=TAGS, summary="Save the guided answers (Supervisor)",
                   description="Body: {observed, feedback_given, changes_assigned, next_focus} (any subset).",
                   operation_id="session_documentation_answers", request=None, responses=JSON_RESPONSE)
    def put(self, request, pk):
        session, error = _load(request, pk)
        if error:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        return _run(request, session, lambda: session_docs.save_answers(session, request.user_account, data, request=request))


class SessionDocumentationGenerateView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Draft or regenerate the outputs from the answers (Supervisor)",
                   description="Body (optional): {kinds: [supervisor_note, supervisee_feedback]}; both when omitted.",
                   operation_id="session_documentation_generate", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk):
        session, error = _load(request, pk)
        if error:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        kinds = data.get("kinds")
        if kinds is not None and not isinstance(kinds, list):
            return Response({"detail": "kinds must be a list."}, status=status.HTTP_400_BAD_REQUEST)
        return _run(request, session, lambda: session_docs.generate(session, request.user_account, kinds, request=request))


class SessionDocumentationOutputView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Edit one output's text (Supervisor; not once approved)",
                   description="Body: {content: {text} | {strengths, improvements, coaching, follow_up}}.",
                   operation_id="session_documentation_edit", request=None, responses=JSON_RESPONSE)
    def patch(self, request, pk, kind):
        session, error = _load(request, pk)
        if error:
            return error
        data = request.data if isinstance(request.data, dict) else {}
        return _run(request, session, lambda: session_docs.edit(session, request.user_account, kind, data.get("content"), request=request))


class SessionDocumentationApproveView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Approve one output (Supervisor). Approved feedback becomes visible to the supervisee.",
                   operation_id="session_documentation_approve", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk, kind):
        session, error = _load(request, pk)
        if error:
            return error
        return _run(request, session, lambda: session_docs.approve(session, request.user_account, kind, request=request))


class SessionDocumentationAcknowledgeView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Acknowledge receipt of approved feedback (the supervisee)",
                   operation_id="session_documentation_acknowledge", request=None, responses=JSON_RESPONSE)
    def post(self, request, pk):
        session, error = _load(request, pk)
        if error:
            return error
        return _run(request, session, lambda: session_docs.acknowledge(session, request.user_account, request=request))


class MyFeedbackView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=TAGS, summary="Approved session feedback shared with me (supervisee workspace)",
                   operation_id="my_session_feedback", responses=JSON_RESPONSE)
    def get(self, request):
        items = session_docs.my_feedback(request.user_account)
        return Response({"feedback": items, "unacknowledged": sum(1 for i in items if i["can_acknowledge"])})
