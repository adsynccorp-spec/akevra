"""M7 Part B: the AI Teaching Assistant (provenance and disposition).

Rules this module keeps (milestone acceptance criteria B1-B5):

* B1  A suggestion is inert. Generating one writes only AISuggestion / AISuggestionEvent rows; nothing in the
      official record (the development plan) changes until the relationship's Supervisor dispositions it.
* B2  Every suggestion keeps an immutable identifier, the exact prompt (or rule input) it came from, the model
      (or rule generator) version and its timestamp. AISuggestion.save() refuses any change to them.
* B3  Disposition is Accept / Edit-and-Accept / Reject. Edit-and-Accept keeps the original generated text in
      `content` (attributed to the model) and the Supervisor's text in `final_content` (attributed to them).
* B4  Nothing is deleted. A new request supersedes the earlier pending suggestions; rejected and superseded ones
      stay listed. A suggestion whose source records changed since generation is stale and can't be accepted.
* B5  An adopted suggestion becomes a goal on a NEW development plan version, and the goal's `ai_suggestion`
      link is carried into every later version (services.snapshot_children).

Clinical guidance (M6): suggestions are reference material only. They never create or modify a
CompetencyAssessment; the only thing an accepted suggestion can add is a development-plan goal.
"""

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
import uuid

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import write_audit
from apps.rbac.services import has_permission
from apps.supervision import services
from apps.supervision.models import (
    COMPETENCY_LEVEL_ORDER,
    COMPLIANCE_BEARING_TRACKS,
    GOAL_CATEGORY_CHOICES,
    AISuggestion,
    AISuggestionEvent,
    Assignment,
    AssessmentStatus,
    CompetencyAssessment,
    CompetencyLevel,
    DevelopmentPlan,
    MonthlyCycle,
    SuggestionDisposition,
)

logger = logging.getLogger(__name__)

RULES_GENERATOR = "akevra-rules/1.0"
API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-sonnet-5-5"
MIN_SUGGESTIONS, MAX_SUGGESTIONS = 2, 4
CONFIDENCE_LEVELS = ("high", "moderate", "low")
ACTIONS = ("accept", "edit_accept", "reject")
ADOPTED = (SuggestionDisposition.ACCEPTED, SuggestionDisposition.EDITED)

_GOAL_CATEGORIES = {value for value, _ in GOAL_CATEGORY_CHOICES}
_LEVEL_LABELS = dict(CompetencyLevel.choices)
# Ratings at or below this need a development goal
_LOW_LEVELS = {CompetencyLevel.NEEDS_TRAINING, CompetencyLevel.DEVELOPING}


class StaleSuggestion(ValidationError):
    """The records a suggestion was generated from changed, or it was superseded."""


# ---------------------------------------------------------------------------
# De-identified context and its fingerprint
# ---------------------------------------------------------------------------

def _current_plan(relationship):
    return (
        DevelopmentPlan.objects.filter(relationship=relationship, is_current=True)
        .prefetch_related("goals__ai_suggestion", "competencies", "milestones")
        .first()
    )


def _compliance_shortfalls(relationship):
    """Unmet checks in the last three completed months (an open month isn't a shortfall yet)."""
    if relationship.supervision_track not in COMPLIANCE_BEARING_TRACKS:
        return []
    from apps.supervision.compliance import organization_today, relationship_compliance

    today = organization_today(relationship.organization)
    cycles = list(MonthlyCycle.objects.filter(relationship=relationship).order_by("year", "month"))
    if not cycles:
        return []
    months = relationship_compliance(relationship, cycles).get("months") or []
    shortfalls = []
    for month in months:
        if (month["year"], month["month"]) >= (today.year, today.month):
            continue
        unmet = sorted({check["label"] for check in month.get("checks") or [] if check.get("status") == "not_met"})
        if unmet:
            shortfalls.append({"month": f"{month['year']:04d}-{month['month']:02d}", "unmet_requirements": unmet})
    return shortfalls[-3:]


def build_context(relationship, *, exclude_batch=None):
    """What a suggestion is generated from. No names or contact details: only the pathway and the
    records' clinical content. `exclude_batch` leaves out goals adopted from that batch, so accepting
    one suggestion doesn't make its siblings from the same request stale."""
    plan = _current_plan(relationship)
    goals, competencies = [], []
    if plan is not None:
        goals = [
            {"category": g.category, "title": g.title, "status": g.status}
            for g in plan.goals.all()
            if not (exclude_batch and g.ai_suggestion_id and g.ai_suggestion.batch_id == exclude_batch)
        ]
        competencies = [
            {"name": c.name, "domain": c.domain, "current_level": c.current_level, "target_level": c.target_level}
            for c in plan.competencies.all()
        ]
    latest = {}
    official = (
        CompetencyAssessment.objects.filter(relationship=relationship, status=AssessmentStatus.OFFICIAL)
        .exclude(record_status="archived")
        .order_by("assessed_on", "created_at")
    )
    for assessment in official:
        latest[assessment.competency_name] = {
            "competency": assessment.competency_name,
            "rating": assessment.rating,
            "assessed_on": assessment.assessed_on.isoformat(),
        }
    statuses = {}
    for status in Assignment.objects.filter(cycle__relationship=relationship).exclude(record_status="archived").values_list("status", flat=True):
        statuses[status] = statuses.get(status, 0) + 1
    return {
        "pathway": relationship.supervision_track,
        "relationship_status": relationship.status,
        "development_plan": {"exists": plan is not None, "goals": goals, "competencies": competencies},
        "official_competency_ratings": [latest[name] for name in sorted(latest)],
        "compliance_shortfalls": _compliance_shortfalls(relationship),
        "assignments_by_status": dict(sorted(statuses.items())),
    }


def context_hash(context):
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def is_stale(suggestion, *, current_hashes=None):
    """Superseded suggestions are always stale. A pending one is stale when its source records changed.
    A dispositioned one was checked when it was decided and keeps that result."""
    if suggestion.disposition == SuggestionDisposition.SUPERSEDED:
        return True
    if suggestion.disposition != SuggestionDisposition.PENDING:
        return False
    key = (suggestion.relationship_id, suggestion.batch_id)
    if current_hashes is not None and key in current_hashes:
        current = current_hashes[key]
    else:
        current = context_hash(build_context(suggestion.relationship, exclude_batch=suggestion.batch_id))
        if current_hashes is not None:
            current_hashes[key] = current
    return current != suggestion.context_hash


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _api_key():
    return getattr(settings, "ANTHROPIC_API_KEY", None) or os.environ.get("ANTHROPIC_API_KEY") or ""


def model_setting():
    return getattr(settings, "AKEVRA_AI_MODEL", None) or os.environ.get("AKEVRA_AI_MODEL") or DEFAULT_MODEL


def generator_info():
    """What will produce the next request's suggestions (shown honestly in the UI)."""
    if _api_key():
        return {"kind": "ai", "name": model_setting(), "ai_configured": True}
    return {"kind": "rules", "name": RULES_GENERATOR, "ai_configured": False}


def _category_for(domain):
    return domain if domain in _GOAL_CATEGORIES else "professional_growth"


def _level(value):
    return _LEVEL_LABELS.get(value, value or "not recorded")


def _rules_generate(context):
    """akevra-rules/1.0: deterministic suggestions from the same de-identified context.
    Each returned item carries the rule that produced it."""
    plan = context["development_plan"]
    goal_text = " ".join(g["title"].lower() for g in plan["goals"])
    ratings = {r["competency"]: r for r in context["official_competency_ratings"]}
    items = []

    # R1: a competency officially rated (or planned) at Needs Training / Developing -> a goal to raise it
    for competency in plan["competencies"]:
        name = competency["name"]
        if name.lower() in goal_text:
            continue
        rating = ratings.get(name)
        level = rating["rating"] if rating else competency["current_level"]
        if level not in _LOW_LEVELS:
            continue
        target = competency["target_level"]
        if target not in COMPETENCY_LEVEL_ORDER:  # blank, or free text from before the fixed scale
            target = CompetencyLevel.PERFORMS_WITH_SUPPORT
        if COMPETENCY_LEVEL_ORDER.index(target) <= COMPETENCY_LEVEL_ORDER.index(level):
            target = COMPETENCY_LEVEL_ORDER[COMPETENCY_LEVEL_ORDER.index(level) + 1]
        evidence = (
            [f"Latest official assessment of {name}: {_level(level)} ({rating['assessed_on']})."]
            if rating else [f"Development plan lists {name} at {_level(level)}; no official assessment yet."]
        )
        if competency["target_level"]:
            evidence.append(f"Plan target level: {_level(competency['target_level'])}.")
        items.append({
            "rule": "R1-low-competency",
            "category": _category_for(competency["domain"]),
            "title": f"Raise {name} from {_level(level)} to {_level(target)}",
            "description": (
                f"Practise {name} with modelling and feedback in supervision, then rate it again on the "
                f"competency scale once it is performed at {_level(target)} across consecutive observations."
            ),
            "evidence": evidence,
            "confidence": "high" if rating else "moderate",
            "explanation": f"{name} is below the level the plan aims for and no goal in the current plan addresses it.",
        })

    # R2: a requirement missed in a completed month -> a goal to meet it
    missed = {}
    for shortfall in context["compliance_shortfalls"]:
        for label in shortfall["unmet_requirements"]:
            missed.setdefault(label, []).append(shortfall["month"])
    for label, months in sorted(missed.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        items.append({
            "rule": "R2-missed-compliance-check",
            "category": "professional_growth",
            "title": f"Meet the monthly requirement: {label}"[:255],
            "description": (
                "Plan supervision contacts and hours at the start of each month and review progress at mid-month "
                f"so that '{label}' is met before the month closes."
            ),
            "evidence": [f"Not met in {month}." for month in months],
            "confidence": "high" if len(months) > 1 else "moderate",
            "explanation": "A compliance requirement was missed in a completed month; a planned goal makes the next month's check routine.",
        })

    # R3: tasks sent back for revision -> a goal on independent task completion
    revisions = context["assignments_by_status"].get("revision_requested", 0)
    if revisions:
        items.append({
            "rule": "R3-revisions-requested",
            "category": "professional_growth",
            "title": "Complete assigned tasks to the stated criteria on first submission",
            "description": "Review each task's requirements before submitting and use a short self-check against them.",
            "evidence": [f"{revisions} assignment(s) currently returned for revision."],
            "confidence": "moderate",
            "explanation": "Tasks are being returned for revision; a goal on meeting the criteria first time addresses the pattern.",
        })

    # R4: planned competencies without any official rating -> a baseline assessment goal
    unrated = [c["name"] for c in plan["competencies"] if c["name"] not in ratings]
    if unrated:
        listed = ", ".join(unrated[:3]) + (" and others" if len(unrated) > 3 else "")
        items.append({
            "rule": "R4-no-official-rating",
            "category": "professional_growth",
            "title": f"Establish an official baseline rating for {listed}"[:255],
            "description": "Observe each competency directly and record an official assessment so progress can be measured.",
            "evidence": [f"No official assessment recorded for: {', '.join(unrated)}."],
            "confidence": "moderate",
            "explanation": "Without a baseline rating, later progress on these competencies can't be shown.",
        })

    # R5 (fallbacks so a request always returns at least two): no plan content yet / review cadence
    if not plan["competencies"]:
        items.append({
            "rule": "R5-no-competencies",
            "category": "professional_growth",
            "title": "Identify the core competencies for this pathway and record a starting level for each",
            "description": "Select competencies from the plan library for this pathway and record where performance stands today.",
            "evidence": ["The current development plan has no competencies." if plan["exists"] else "No development plan exists yet."],
            "confidence": "low",
            "explanation": "Suggestions are more specific once competencies and ratings exist to draw on.",
        })
    items.append({
        "rule": "R6-review-cadence",
        "category": "professional_growth",
        "title": "Review progress on each development goal with the supervisor every month",
        "description": "At one supervision contact each month, rate progress on every active goal and agree the next step.",
        "evidence": [f"{len(plan['goals'])} goal(s) in the current plan."],
        "confidence": "low",
        "explanation": "A regular review keeps the plan current and shows when a goal is met.",
    })

    seen, unique = set(), []
    for item in items:
        if item["title"] not in seen:
            seen.add(item["title"])
            unique.append(item)
    return unique[:MAX_SUGGESTIONS]


_AI_INSTRUCTIONS = """You are assisting a BACB-certified Supervisor who is writing an individualized development plan.
Suggest between 2 and 4 development-plan goals for the supervisee, based only on the de-identified records below.
Suggestions are reference material for the Supervisor to accept, edit or reject; they never change any record by themselves.
Do not suggest changes to competency ratings or assessments.

Reply with strict JSON only, no prose and no code fences, in exactly this shape:
{"suggestions": [{"category": "<one of: %(categories)s>", "title": "<observable, measurable goal, max 200 characters>",
"description": "<how the goal would be worked on and measured>", "evidence": ["<the specific record each point comes from>"],
"confidence": "<high|moderate|low>", "explanation": "<why this goal follows from the records>"}]}

Records:
%(context)s"""


def _ai_prompt(context):
    return _AI_INSTRUCTIONS % {
        "categories": ", ".join(sorted(_GOAL_CATEGORIES)),
        "context": json.dumps(context, sort_keys=True, indent=2),
    }


def _parse_ai_items(text):
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1])
    items = []
    for raw in data["suggestions"][:MAX_SUGGESTIONS]:
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        category = raw.get("category") if raw.get("category") in _GOAL_CATEGORIES else "other"
        confidence = raw.get("confidence") if raw.get("confidence") in CONFIDENCE_LEVELS else "low"
        evidence = raw.get("evidence") or []
        items.append({
            "category": category,
            "title": title[:255],
            "description": str(raw.get("description") or "").strip(),
            "evidence": [str(e) for e in evidence] if isinstance(evidence, list) else [str(evidence)],
            "confidence": confidence,
            "explanation": str(raw.get("explanation") or "").strip() or "No explanation was given by the model.",
        })
    if len(items) < MIN_SUGGESTIONS:
        raise ValueError("The model returned fewer than two usable suggestions.")
    return items


def _ai_generate(context, key):
    """Anthropic Messages API over HTTPS (urllib, no SDK dependency). Returns (prompt, model_id, items)."""
    prompt = _ai_prompt(context)
    body = json.dumps({
        "model": model_setting(),
        "max_tokens": 4000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    request = urllib.request.Request(API_URL, data=body, method="POST", headers={
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    })
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 (fixed https URL)
        payload = json.loads(response.read().decode())
    if payload.get("stop_reason") == "refusal":
        raise ValueError("The model declined the request.")
    text = "".join(block.get("text", "") for block in payload.get("content", []) if block.get("type") == "text")
    return prompt, payload.get("model") or model_setting(), _parse_ai_items(text)


def _rules_prompt(context, items):
    """For the rule generator the 'prompt' is the serialized rule input: generator, the rules that fired, the context."""
    return json.dumps(
        {"generator": RULES_GENERATOR, "rules_fired": [item["rule"] for item in items], "context": context},
        sort_keys=True, indent=2,
    )


def _generate_items(context):
    key = _api_key()
    if key:
        try:
            return _ai_generate(context, key)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning("AI suggestion request failed, using %s instead: %s", RULES_GENERATOR, exc)
    items = _rules_generate(context)
    return _rules_prompt(context, items), RULES_GENERATOR, items


# ---------------------------------------------------------------------------
# Generate and disposition
# ---------------------------------------------------------------------------

def _event(suggestion, action, actor, note="", **detail):
    return AISuggestionEvent.objects.create(
        organization_id=suggestion.organization_id, suggestion=suggestion, action=action,
        actor=actor, note=note, detail=detail,
    )


def _audit_summary(suggestion):
    return {
        "suggestion_identifier": str(suggestion.suggestion_identifier),
        "model_name": suggestion.model_name,
        "disposition": suggestion.disposition,
        "content": suggestion.content,
        "final_content": suggestion.final_content,
        "disposition_note": suggestion.disposition_note,
    }


def generate(relationship, actor, *, request=None):
    """Creates one batch of 2-4 pending suggestions and supersedes earlier pending ones. Writes nothing
    to the development plan (B1)."""
    if not has_permission(actor, "idp.manage", relationship=relationship):
        raise PermissionDenied("Only the relationship's Supervisor can request AI suggestions.")
    if relationship.status != "active":
        raise ValidationError("Suggestions can only be requested for an active relationship.")
    context = build_context(relationship)
    fingerprint = context_hash(context)
    prompt, model_name, items = _generate_items(context)  # outside the transaction: may call the network
    batch_id = uuid.uuid4()
    with transaction.atomic():
        created = [
            AISuggestion.objects.create(
                organization_id=relationship.organization_id,
                relationship=relationship,
                suggestion_identifier=uuid.uuid4(),
                prompt=prompt,
                model_name=model_name,
                explanation=item["explanation"],
                content={key: item[key] for key in ("category", "title", "description", "evidence", "confidence")},
                context_hash=fingerprint,
                batch_id=batch_id,
                requested_by=actor,
            )
            for item in items
        ]
        earlier = (
            AISuggestion.objects.select_for_update()
            .filter(relationship=relationship, disposition=SuggestionDisposition.PENDING)
            .exclude(batch_id=batch_id)
        )
        for old in earlier:
            old.disposition = SuggestionDisposition.SUPERSEDED
            old.superseded_by = created[0]
            old.save(update_fields=["disposition", "superseded_by", "updated_at"])
            _event(old, "superseded", actor, "A newer request replaced this suggestion.",
                   superseded_by_batch=str(batch_id), superseded_by=str(created[0].suggestion_identifier))
            write_audit(action="ai_suggestion.supersede", entity_type="ai_suggestion", entity_id=old.id,
                        request=request, organization=relationship.organization, after=_audit_summary(old))
        for suggestion in created:
            _event(suggestion, "generated", actor, model_name=model_name, context_hash=fingerprint, batch_id=str(batch_id))
            write_audit(action="ai_suggestion.generate", entity_type="ai_suggestion", entity_id=suggestion.id,
                        request=request, organization=relationship.organization, after=_audit_summary(suggestion),
                        metadata={"relationship_id": str(relationship.id), "batch_id": str(batch_id)})
    return created


def _final_from(original, submitted):
    if not isinstance(submitted, dict):
        raise ValidationError("Edit & Accept needs the final goal text.")
    final = {
        "category": submitted.get("category", original.get("category")) or "",
        "title": str(submitted.get("title") or "").strip(),
        "description": str(submitted.get("description") or "").strip(),
    }
    if not final["title"]:
        raise ValidationError("Edit & Accept needs a goal statement.")
    if len(final["title"]) > 255:
        raise ValidationError("The goal statement can be at most 255 characters.")
    if final["category"] and final["category"] not in _GOAL_CATEGORIES:
        raise ValidationError(f"Unknown goal category '{final['category']}'.")
    if all(final[key] == (original.get(key) or "") for key in final):
        raise ValidationError("Nothing was changed. Use Accept to adopt the suggestion as written.")
    return final


def disposition(suggestion, actor, action, *, final_content=None, note="", request=None):
    """Accept / Edit-and-Accept / Reject a pending suggestion. Accepting adds the goal to the development
    plan as a NEW plan version (the earlier version is kept), with the goal linked to the suggestion."""
    if action not in ACTIONS:
        raise ValidationError("Action must be accept, edit_accept or reject.")
    note = (note or "").strip()
    with transaction.atomic():
        suggestion = AISuggestion.objects.select_for_update().select_related("relationship", "relationship__organization").get(pk=suggestion.pk)
        relationship = suggestion.relationship
        if not has_permission(actor, "idp.manage", relationship=relationship):
            raise PermissionDenied("Only the relationship's Supervisor can review AI suggestions.")
        if suggestion.disposition == SuggestionDisposition.SUPERSEDED:
            raise StaleSuggestion("This suggestion was superseded by a newer request and can't be reviewed.")
        if suggestion.disposition != SuggestionDisposition.PENDING:
            raise ValidationError("This suggestion has already been reviewed.")

        if action == "reject":
            if not note:
                raise ValidationError("Give a reason for rejecting the suggestion.")
            suggestion.disposition = SuggestionDisposition.REJECTED
            suggestion.dispositioned_by = actor
            suggestion.dispositioned_at = timezone.now()
            suggestion.disposition_note = note
            suggestion.save(update_fields=["disposition", "dispositioned_by", "dispositioned_at", "disposition_note", "updated_at"])
            _event(suggestion, "rejected", actor, note)
            write_audit(action="ai_suggestion.reject", entity_type="ai_suggestion", entity_id=suggestion.id,
                        request=request, organization=relationship.organization, after=_audit_summary(suggestion))
            return suggestion

        if is_stale(suggestion):
            raise StaleSuggestion(
                "The records this suggestion was generated from have changed since. Request new suggestions instead."
            )
        if relationship.status != "active":
            raise ValidationError("Suggestions can only be adopted on an active relationship.")
        original = suggestion.content or {}
        if action == "edit_accept":
            final = _final_from(original, final_content)
            suggestion.final_content = final
            suggestion.disposition = SuggestionDisposition.EDITED
        else:
            final = {key: original.get(key) or "" for key in ("category", "title", "description")}
            suggestion.disposition = SuggestionDisposition.ACCEPTED
        suggestion.dispositioned_by = actor
        suggestion.dispositioned_at = timezone.now()
        suggestion.disposition_note = note
        # Recorded before the plan changes so the plan service accepts the goal's link to it
        suggestion.save(update_fields=["disposition", "final_content", "dispositioned_by", "dispositioned_at",
                                       "disposition_note", "updated_at"])

        goal = {**final, "status": "active", "ai_suggestion_id": suggestion.id}
        current = DevelopmentPlan.objects.filter(relationship=relationship, is_current=True).first()
        if current is None:
            prior = None
            plan = services.create_development_plan(
                organization=relationship.organization, relationship=relationship, goals=[goal]
            )
        else:
            prior = services.serialize_plan(current)
            existing = services.snapshot_children(current)["goals"]
            plan = services.revise_development_plan(current, goals=existing + [{**goal, "sort_order": len(existing)}])
        new_goal = plan.goals.get(ai_suggestion=suggestion)
        write_audit(action="idp.revise" if prior else "create", entity_type="development_plan", entity_id=plan.id,
                    request=request, organization=relationship.organization, before=prior,
                    after=services.serialize_plan(plan),
                    metadata={"source": "ai_suggestion", "suggestion_identifier": str(suggestion.suggestion_identifier),
                              "new_version": plan.version_number})
        _event(suggestion, "edited_accepted" if action == "edit_accept" else "accepted", actor, note,
               plan_id=str(plan.id), plan_version=plan.version_number, goal_id=str(new_goal.id),
               final_content=final)
        write_audit(action=f"ai_suggestion.{action}", entity_type="ai_suggestion", entity_id=suggestion.id,
                    request=request, organization=relationship.organization, after=_audit_summary(suggestion),
                    metadata={"plan_id": str(plan.id), "goal_id": str(new_goal.id)})
    return suggestion


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

STATUS_LABELS = {
    SuggestionDisposition.ACCEPTED: "Accepted as written",
    SuggestionDisposition.EDITED: "Edited and accepted",
    SuggestionDisposition.REJECTED: "Rejected",
    SuggestionDisposition.SUPERSEDED: "Superseded by a newer request (not current)",
}


def _person(user):
    return None if user is None else {"id": str(user.id), "name": user.full_name}


def serialize_suggestion(suggestion, *, current_hashes=None, include_prompt=True):
    stale = is_stale(suggestion, current_hashes=current_hashes)
    if suggestion.disposition == SuggestionDisposition.PENDING:
        label = "Out of date: records changed since it was generated (not current)" if stale else "Awaiting review"
    else:
        label = STATUS_LABELS.get(suggestion.disposition, suggestion.disposition)
    rel = suggestion.relationship
    adopted = sorted(suggestion.adopted_goals.all(), key=lambda g: g.plan.version_number)
    data = {
        "id": str(suggestion.id),
        "identifier": str(suggestion.suggestion_identifier),
        "relationship_id": str(rel.id),
        "supervisee": {"id": str(rel.supervisee_id), "name": rel.supervisee.full_name},
        "supervision_track": rel.supervision_track,
        "batch_id": str(suggestion.batch_id) if suggestion.batch_id else None,
        "model_name": suggestion.model_name,
        "generator": "rules" if suggestion.model_name.startswith("akevra-rules/") else "ai",
        "created_at": suggestion.created_at.isoformat(),
        "requested_by": _person(suggestion.requested_by),
        "context_hash": suggestion.context_hash,
        "explanation": suggestion.explanation,
        "content": suggestion.content,
        "final_content": suggestion.final_content,
        "disposition": suggestion.disposition,
        "stale": stale,
        # Only a pending suggestion whose records haven't changed is ever "current"
        "is_current": suggestion.disposition == SuggestionDisposition.PENDING and not stale,
        "status_label": label,
        "dispositioned_by": _person(suggestion.dispositioned_by),
        "dispositioned_at": suggestion.dispositioned_at.isoformat() if suggestion.dispositioned_at else None,
        "disposition_note": suggestion.disposition_note,
        "superseded_by": str(suggestion.superseded_by.suggestion_identifier) if suggestion.superseded_by_id else None,
        # B3: each text with its author
        "attribution": {
            "original": {"author": suggestion.model_name, "kind": "generated", "at": suggestion.created_at.isoformat(),
                         "content": suggestion.content},
            "final": None if suggestion.disposition not in ADOPTED else {
                "author": _person(suggestion.dispositioned_by), "kind": "supervisor",
                "at": suggestion.dispositioned_at.isoformat() if suggestion.dispositioned_at else None,
                "content": suggestion.final_content or {k: (suggestion.content or {}).get(k) for k in ("category", "title", "description")},
                "edited": suggestion.disposition == SuggestionDisposition.EDITED,
            },
        },
        # B5: every plan version the adopted goal appears in
        "adopted_goals": [
            {"goal_id": str(g.id), "plan_id": str(g.plan_id), "plan_version": g.plan.version_number,
             "plan_is_current": g.plan.is_current, "title": g.title}
            for g in adopted
        ],
        "events": [
            {"action": e.action, "actor": _person(e.actor), "note": e.note, "detail": e.detail, "at": e.created_at.isoformat()}
            for e in sorted(suggestion.events.all(), key=lambda e: e.created_at)
        ],
    }
    if include_prompt:
        data["prompt"] = suggestion.prompt
    return data
