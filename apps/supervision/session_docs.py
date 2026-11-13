"""Guided, AI-assisted session documentation (AI Supervision Enhancement, session workflow).

Rules this module keeps (acceptance criteria):

1. One workflow per session: the four guided answers and both outputs hang off the session record itself
   (SessionDocumentation is one-to-one with SupervisionSession), so nothing is entered twice.
2. Nothing is official or visible to the supervisee until the Supervisor approves THAT output. Each output
   (private supervisor note, supervisee feedback summary) is drafted, edited, regenerated and approved on its own.
3. The supervisee feedback is visible only to the relationship's own supervisee (and its Supervisor), and only
   once approved. The supervisor note is never shown to anyone but the Supervisor of record.
4. Every generated draft keeps an immutable identifier, the exact prompt (or template input), model version and
   timestamp; every edit, regeneration, approval and acknowledgment is an event with its actor.
5. The supervisee's acknowledgment of receipt is recorded separately from the Supervisor's approval.

What is sent to the AI provider follows the AI Teaching Assistant safeguards: only when the organization turned
AI on, and only after redaction (apps.supervision.ai.redact_for_ai).
"""

import hashlib
import json
import logging
import urllib.error
import urllib.request
import uuid

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import write_audit
from apps.rbac.services import has_permission
from apps.supervision import ai
from apps.supervision.models import (
    SessionDocDraft,
    SessionDocEvent,
    SessionDocKind,
    SessionDocOutput,
    SessionDocStatus,
    SessionDocumentation,
    SessionStatus,
)

logger = logging.getLogger(__name__)

TEMPLATE_GENERATOR = "akevra-doc-template/1.0"
QUESTIONS = (
    ("observed", "What did you observe?"),
    ("feedback_given", "What feedback, coaching or modelling did you give?"),
    ("changes_assigned", "What changes or new tasks did you assign?"),
    ("next_focus", "What is the focus for next time?"),
)
ANSWER_KEYS = tuple(key for key, _ in QUESTIONS)
FIELDS = {
    SessionDocKind.SUPERVISOR_NOTE: ("text",),
    SessionDocKind.SUPERVISEE_FEEDBACK: ("strengths", "improvements", "coaching", "follow_up"),
}
MAX_ANSWER = 4000
MAX_FIELD = 6000


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def _relationship(session):
    return session.cycle.relationship


def can_document(user, session):
    return has_permission(user, "session.document", relationship=_relationship(session))


def is_own_supervisee(user, session):
    return user is not None and _relationship(session).supervisee_id == user.id


def _require_supervisor(user, session):
    if not can_document(user, session):
        raise PermissionDenied("Only the relationship's Supervisor can document this session.")


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

def build_context(documentation):
    session = documentation.session
    rel = _relationship(session)
    return {
        "pathway": rel.supervision_track,
        "session": {
            "month": session.occurred_on.strftime("%Y-%m"),
            "duration_minutes": session.duration_minutes,
            "type": session.session_type,
            "meeting_method": session.meeting_method,
            "client_observation": session.client_observation,
        },
        "supervisor_answers": {key: getattr(documentation, key) for key in ANSWER_KEYS},
    }


def _template(context, kind):
    """akevra-doc-template/1.0: arranges the Supervisor's own answers; adds nothing of its own."""
    a = context["supervisor_answers"]
    s = context["session"]
    if kind == SessionDocKind.SUPERVISOR_NOTE:
        lines = [f"Supervision session ({s['type']}, {s['duration_minutes']} min, {s['month']})."]
        for key, label in (("observed", "Observed"), ("feedback_given", "Feedback and coaching provided"),
                           ("changes_assigned", "Changes assigned"), ("next_focus", "Next focus")):
            if a[key].strip():
                lines.append(f"{label}: {a[key].strip()}")
        return {"text": "\n".join(lines)}
    return {
        "strengths": a["observed"].strip(),
        "improvements": a["next_focus"].strip(),
        "coaching": a["feedback_given"].strip(),
        "follow_up": a["changes_assigned"].strip(),
    }


_INSTRUCTIONS = """You are helping a BACB-certified Supervisor document a supervision session they just held.
Use only the Supervisor's own answers and session details below; do not invent observations, data, scores,
dates, names or instructions that are not there. Some text may read [REDACTED]; never guess what it was.
Write in plain professional language.

Draft %(what)s.
- "supervisor_note": the Supervisor's private session note, a short narrative of what was observed, the feedback
  and coaching given, changes assigned and the next focus.
- "supervisee_feedback": a supportive summary written to the supervisee, with four parts: strengths observed,
  areas to improve, the coaching given, and follow-up actions.

Reply with strict JSON only, no prose and no code fences, in exactly this shape:
%(shape)s

Session details and the Supervisor's answers:
%(context)s"""

_SHAPES = {
    SessionDocKind.SUPERVISOR_NOTE: '"supervisor_note": {"text": "<note>"}',
    SessionDocKind.SUPERVISEE_FEEDBACK: '"supervisee_feedback": {"strengths": "<...>", "improvements": "<...>", '
                                        '"coaching": "<...>", "follow_up": "<...>"}',
}


def _prompt(context, kinds):
    return _INSTRUCTIONS % {
        "what": " and ".join(f'"{k}"' for k in kinds),
        "shape": "{" + ", ".join(_SHAPES[k] for k in kinds) + "}",
        "context": json.dumps(context, sort_keys=True, indent=2),
    }


def _clean(kind, raw):
    if not isinstance(raw, dict):
        raise ValueError(f"The model returned no {kind}.")
    out = {field: str(raw.get(field) or "").strip()[:MAX_FIELD] for field in FIELDS[kind]}
    if not any(out.values()):
        raise ValueError(f"The model returned an empty {kind}.")
    return out


def _ai_drafts(context, kinds, key):
    prompt = _prompt(context, kinds)
    body = json.dumps({
        "model": ai.model_setting(),
        "max_tokens": 8000,
        "output_config": {"effort": "medium"},
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(ai.API_URL, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": ai.API_VERSION, "content-type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=90) as response:  # noqa: S310 (fixed https URL)
        payload = json.loads(response.read().decode())
    if payload.get("stop_reason") in ("refusal", "max_tokens"):
        raise ValueError(f"The model stopped early ({payload.get('stop_reason')}).")
    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    data = json.loads(text[text.find("{"): text.rfind("}") + 1])
    return prompt, payload.get("model") or ai.model_setting(), {k: _clean(k, data.get(k)) for k in kinds}


def _generate(documentation, kinds):
    """(prompt, model_name, {kind: content}, redactions). The AI model only when the organization opted in."""
    context = build_context(documentation)
    organization = _relationship(documentation.session).organization
    key = ai._api_key()
    if key and organization.ai_enabled:
        safe, redactions = ai.redact_for_ai(context, organization)
        try:
            return (*_ai_drafts(safe, kinds, key), redactions)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("AI session drafting failed, using %s instead: %s", TEMPLATE_GENERATOR, exc)
    prompt = json.dumps({"generator": TEMPLATE_GENERATOR, "kinds": list(kinds), "context": context}, sort_keys=True, indent=2)
    return prompt, TEMPLATE_GENERATOR, {k: _template(context, k) for k in kinds}, 0


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------

def _event(documentation, action, actor, kind="", **detail):
    return SessionDocEvent.objects.create(organization_id=documentation.organization_id, documentation=documentation,
                                          kind=kind, action=action, actor=actor, detail=detail)


def _fingerprint(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()[:16]


def _audit(documentation, action, request, **metadata):
    # Metadata only: the private note's text never goes into the audit log's readable fields
    write_audit(action=f"session_documentation.{action}", entity_type="supervision_session",
                entity_id=documentation.session_id, request=request,
                organization=_relationship(documentation.session).organization,
                metadata={"documentation_id": str(documentation.id), **metadata})


def _locked(session):
    return SessionDocumentation.objects.select_for_update().filter(session=session).first()


def save_answers(session, actor, answers, *, request=None):
    _require_supervisor(actor, session)
    if not isinstance(answers, dict):
        raise ValidationError("Send the answers as an object.")
    clean = {}
    for key in ANSWER_KEYS:
        if key in answers:
            value = str(answers.get(key) or "").strip()
            if len(value) > MAX_ANSWER:
                raise ValidationError(f"Keep each answer under {MAX_ANSWER} characters.")
            clean[key] = value
    with transaction.atomic():
        documentation = _locked(session)
        created = documentation is None
        if created:
            documentation = SessionDocumentation.objects.create(
                organization_id=session.organization_id, session=session, updated_by=actor, **clean)
        else:
            for key, value in clean.items():
                setattr(documentation, key, value)
            documentation.updated_by = actor
            documentation.save()
        _event(documentation, "answers_saved", actor, answers={k: getattr(documentation, k) for k in ANSWER_KEYS})
        _audit(documentation, "answers", request, created=created)
    return documentation


def generate(session, actor, kinds=None, *, request=None):
    """Drafts (or regenerates) the requested outputs. An approved output is never overwritten."""
    _require_supervisor(actor, session)
    kinds = list(kinds or FIELDS)
    if any(k not in FIELDS for k in kinds):
        raise ValidationError("Outputs are supervisor_note and supervisee_feedback.")
    documentation = SessionDocumentation.objects.filter(session=session).first()
    if documentation is None or not any(getattr(documentation, k).strip() for k in ANSWER_KEYS):
        raise ValidationError("Answer the guided questions before drafting.")
    approved = set(documentation.outputs.filter(status=SessionDocStatus.APPROVED).values_list("kind", flat=True))
    if approved & set(kinds):
        raise ValidationError("An approved output can't be regenerated.")
    prompt, model_name, contents, redactions = _generate(documentation, kinds)  # may call the network
    with transaction.atomic():
        documentation = _locked(session)
        drafts = []
        for kind in kinds:
            draft = SessionDocDraft.objects.create(
                organization_id=documentation.organization_id, documentation=documentation, kind=kind,
                identifier=uuid.uuid4(), prompt=prompt, model_name=model_name, content=contents[kind],
                redactions=redactions, requested_by=actor,
            )
            output, created = SessionDocOutput.objects.select_for_update().get_or_create(
                documentation=documentation, kind=kind,
                defaults={"organization_id": documentation.organization_id, "content": draft.content, "source_draft": draft},
            )
            if output.status == SessionDocStatus.APPROVED:
                raise ValidationError("An approved output can't be regenerated.")
            if not created:
                output.content, output.source_draft, output.edited = draft.content, draft, False
                output.save(update_fields=["content", "source_draft", "edited", "updated_at"])
            _event(documentation, "generated" if created else "regenerated", actor, kind,
                   draft_identifier=str(draft.identifier), model_name=model_name, redactions=redactions)
            _audit(documentation, "generate", request, kind=kind, draft_identifier=str(draft.identifier), model_name=model_name)
            drafts.append(draft)
    return drafts


def _output(documentation, kind):
    if kind not in FIELDS:
        raise ValidationError("Outputs are supervisor_note and supervisee_feedback.")
    output = SessionDocOutput.objects.select_for_update().filter(documentation=documentation, kind=kind).first()
    if output is None:
        raise ValidationError("Draft this output first.")
    return output


def edit(session, actor, kind, content, *, request=None):
    _require_supervisor(actor, session)
    if not isinstance(content, dict):
        raise ValidationError("Send the output's text.")
    with transaction.atomic():
        documentation = _locked(session)
        if documentation is None:
            raise ValidationError("Draft this output first.")
        output = _output(documentation, kind)
        if output.status == SessionDocStatus.APPROVED:
            raise ValidationError("This output is approved and can no longer be edited.")
        final = {field: str(content.get(field, output.content.get(field, "")) or "").strip() for field in FIELDS[kind]}
        if any(len(v) > MAX_FIELD for v in final.values()):
            raise ValidationError(f"Keep each part under {MAX_FIELD} characters.")
        if final == output.content:
            return output
        before = output.content
        output.content, output.edited = final, True
        output.save(update_fields=["content", "edited", "updated_at"])
        _event(documentation, "edited", actor, kind, before=before, after=final)
        _audit(documentation, "edit", request, kind=kind, fingerprint=_fingerprint(final))
    return output


def approve(session, actor, kind, *, request=None):
    _require_supervisor(actor, session)
    with transaction.atomic():
        documentation = _locked(session)
        if documentation is None:
            raise ValidationError("Draft this output first.")
        output = _output(documentation, kind)
        if output.status == SessionDocStatus.APPROVED:
            raise ValidationError("This output is already approved.")
        if not any(str(v).strip() for v in output.content.values()):
            raise ValidationError("An empty output can't be approved.")
        if kind == SessionDocKind.SUPERVISEE_FEEDBACK and session.status != SessionStatus.COMPLETED:
            raise ValidationError("Complete the session record before sharing feedback with the supervisee.")
        output.status, output.approved_by, output.approved_at = SessionDocStatus.APPROVED, actor, timezone.now()
        output.save(update_fields=["status", "approved_by", "approved_at", "updated_at"])
        _event(documentation, "approved", actor, kind, fingerprint=_fingerprint(output.content), edited=output.edited,
               draft_identifier=str(output.source_draft.identifier) if output.source_draft_id else None)
        _audit(documentation, "approve", request, kind=kind, fingerprint=_fingerprint(output.content))
    return output


def acknowledge(session, actor, *, request=None):
    """The supervisee confirms they received the approved feedback (separate from the approval)."""
    if not is_own_supervisee(actor, session):
        raise PermissionDenied("Only the supervisee can acknowledge their feedback.")
    with transaction.atomic():
        documentation = _locked(session)
        output = documentation and SessionDocOutput.objects.select_for_update().filter(
            documentation=documentation, kind=SessionDocKind.SUPERVISEE_FEEDBACK, status=SessionDocStatus.APPROVED).first()
        if not output:
            raise ValidationError("There is no approved feedback to acknowledge.")
        if output.acknowledged_at:
            raise ValidationError("Already acknowledged.")
        output.acknowledged_by, output.acknowledged_at = actor, timezone.now()
        output.save(update_fields=["acknowledged_by", "acknowledged_at", "updated_at"])
        _event(documentation, "acknowledged", actor, SessionDocKind.SUPERVISEE_FEEDBACK)
        _audit(documentation, "acknowledge", request, kind=SessionDocKind.SUPERVISEE_FEEDBACK)
    return output


# ---------------------------------------------------------------------------
# Serialization (what each viewer may see)
# ---------------------------------------------------------------------------

def _person(user):
    return None if user is None else {"id": str(user.id), "name": user.full_name}


def _at(value):
    return value.isoformat() if value else None


def _draft(draft, *, include_prompt):
    data = {"identifier": str(draft.identifier), "kind": draft.kind, "model_name": draft.model_name,
            "generator": "template" if draft.model_name == TEMPLATE_GENERATOR else "ai",
            "created_at": _at(draft.created_at), "requested_by": _person(draft.requested_by),
            "content": draft.content, "redactions": draft.redactions}
    if include_prompt:
        data["prompt"] = draft.prompt
    return data


def _output_data(output, *, full):
    data = {"kind": output.kind, "status": output.status, "content": output.content,
            "approved_by": _person(output.approved_by), "approved_at": _at(output.approved_at),
            "acknowledged_by": _person(output.acknowledged_by), "acknowledged_at": _at(output.acknowledged_at)}
    if full:
        data.update({"edited": output.edited,
                     "source_draft": str(output.source_draft.identifier) if output.source_draft_id else None,
                     "source_model": output.source_draft.model_name if output.source_draft_id else None})
    return data


def serialize(session, viewer):
    """Supervisor of record: everything. The relationship's supervisee: the approved feedback only.
    Anyone else who may view the session (oversight): status only, no text."""
    documentation = (SessionDocumentation.objects.filter(session=session)
                     .prefetch_related("outputs__approved_by", "outputs__acknowledged_by", "outputs__source_draft",
                                       "drafts__requested_by", "events__actor").first())
    supervisor = can_document(viewer, session)
    supervisee = is_own_supervisee(viewer, session)
    base = {"session_id": str(session.id), "questions": [{"key": k, "label": label} for k, label in QUESTIONS],
            "can_document": supervisor, "can_acknowledge": False,
            "generator": ai.generator_info(_relationship(session).organization) if supervisor else None}
    if documentation is None:
        return {**base, "exists": False, "outputs": {}}
    outputs = {o.kind: o for o in documentation.outputs.all()}
    if supervisor:
        return {
            **base, "exists": True, "id": str(documentation.id),
            "answers": {k: getattr(documentation, k) for k in ANSWER_KEYS},
            "outputs": {kind: _output_data(o, full=True) for kind, o in outputs.items()},
            "drafts": [_draft(d, include_prompt=True) for d in documentation.drafts.all()],
            "events": [{"action": e.action, "kind": e.kind, "actor": _person(e.actor), "detail": e.detail,
                        "at": _at(e.created_at)} for e in documentation.events.all()],
        }
    feedback = outputs.get(SessionDocKind.SUPERVISEE_FEEDBACK)
    shared = feedback if feedback and feedback.status == SessionDocStatus.APPROVED else None
    if supervisee:
        return {**base, "exists": True,
                "outputs": {SessionDocKind.SUPERVISEE_FEEDBACK: _output_data(shared, full=False)} if shared else {},
                "can_acknowledge": bool(shared and not shared.acknowledged_at)}
    return {**base, "exists": True,
            "outputs": {kind: {"kind": kind, "status": o.status, "approved_at": _at(o.approved_at),
                               "acknowledged_at": _at(o.acknowledged_at)} for kind, o in outputs.items()}}


def my_feedback(supervisee):
    """Approved feedback shared with this supervisee (their workspace), newest session first."""
    outputs = (SessionDocOutput.objects
               .filter(kind=SessionDocKind.SUPERVISEE_FEEDBACK, status=SessionDocStatus.APPROVED,
                       documentation__session__cycle__relationship__supervisee=supervisee)
               .select_related("documentation__session__cycle__relationship__supervisor", "approved_by", "acknowledged_by")
               .order_by("-documentation__session__occurred_on", "-approved_at"))
    items = []
    for o in outputs:
        session = o.documentation.session
        rel = session.cycle.relationship
        items.append({"session_id": str(session.id), "occurred_on": session.occurred_on.isoformat(),
                      "duration_minutes": session.duration_minutes, "session_type": session.session_type,
                      "relationship_id": str(rel.id), "supervisor": _person(rel.supervisor),
                      **_output_data(o, full=False), "can_acknowledge": not o.acknowledged_at})
    return items
