import copy
import uuid

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from apps.core.models import RecordStatus
from apps.supervision.models import (
    COMPETENCY_LEVEL_ORDER,
    DEVELOPMENT_DOMAIN_CHOICES,
    GOAL_CATEGORY_CHOICES,
    TARGET_LEVELS,
    CompetencyLevel,
    DevelopmentPlan,
    DevelopmentPlanCompetency,
    DevelopmentPlanGoal,
    DevelopmentPlanMilestone,
    SuperviseeIntake,
)
from apps.supervision.plan_library import MILESTONE_TEMPLATE_KEYS, find_competency


class IntakeAlreadyExists(ValidationError):
    """Exactly one intake record is allowed per relationship."""


class PlanContentImmutable(ValidationError):
    """Prior IDP versions cannot be overwritten."""


INTAKE_FIELDS = (
    "prior_aba_experience",
    "prior_aba_experience_other",
    "current_context",
    "current_context_other",
    "background",
    "starting_domains",
    "starting_domains_other",
    "further_assessment_needed",
    "starting_notes",
    "notes",
)


def create_intake(*, organization, relationship, captured_on, **fields):
    """The credential is always copied from the supervisee's profile (the source of truth),
    so the intake can never disagree with the relationship's pathway.
    `fields` takes any of INTAKE_FIELDS; choices and "Other" text are validated by the model."""
    unknown = set(fields) - set(INTAKE_FIELDS)
    if unknown:
        raise ValidationError(f"Unknown intake fields: {', '.join(sorted(unknown))}.")
    if SuperviseeIntake.objects.filter(relationship=relationship).exists():
        raise IntakeAlreadyExists("A supervisee intake already exists for this relationship.")
    values = {key: value for key, value in fields.items() if value is not None}
    try:
        return SuperviseeIntake.objects.create(
            organization=organization,
            relationship=relationship,
            captured_on=captured_on,
            current_credential=relationship.supervisee.credential_type,
            **values,
        )
    except IntegrityError as exc:
        raise IntakeAlreadyExists(
            "A supervisee intake already exists for this relationship."
        ) from exc


def serialize_intake(intake):
    return {
        "id": str(intake.id),
        "relationship_id": str(intake.relationship_id),
        "prior_aba_experience": intake.prior_aba_experience,
        "prior_aba_experience_other": intake.prior_aba_experience_other,
        "current_context": intake.current_context,
        "current_context_other": intake.current_context_other,
        "background": intake.background,
        "current_credential": intake.current_credential,
        "starting_domains": intake.starting_domains,
        "starting_domains_other": intake.starting_domains_other,
        "further_assessment_needed": intake.further_assessment_needed,
        "starting_notes": intake.starting_notes,
        "notes": intake.notes,
        "captured_on": intake.captured_on.isoformat(),
        "created_at": intake.created_at.isoformat(),
    }


def _serialize_goal(goal):
    return {
        "id": str(goal.id),
        "category": goal.category,
        "title": goal.title,
        "description": goal.description,
        "status": goal.status,
        "sort_order": goal.sort_order,
    }


def _serialize_competency(item):
    return {
        "id": str(item.id),
        "name": item.name,
        "competency_key": item.competency_key,
        "domain": item.domain,
        "pathways": item.pathways,
        "description": item.description,
        "target_level": item.target_level,
        "current_level": item.current_level,
        "sort_order": item.sort_order,
    }


def _serialize_milestone(item):
    return {
        "id": str(item.id),
        "title": item.title,
        "template_key": item.template_key,
        "criterion_count": item.criterion_count,
        "status": item.status,
        "rationale": item.rationale,
        "sort_order": item.sort_order,
    }


def serialize_plan(plan, *, include_children=True):
    data = {
        "id": str(plan.id),
        "relationship_id": str(plan.relationship_id),
        "plan_family_id": str(plan.plan_family_id),
        "version_number": plan.version_number,
        "is_current": plan.is_current,
        "record_status": plan.record_status,
        "summary": plan.summary,
        "created_at": plan.created_at.isoformat(),
        "updated_at": plan.updated_at.isoformat(),
    }
    if include_children:
        data["goals"] = [_serialize_goal(g) for g in plan.goals.all()]
        data["competencies"] = [_serialize_competency(c) for c in plan.competencies.all()]
        data["milestones"] = [_serialize_milestone(m) for m in plan.milestones.all()]
    return data


def _create_children(plan, *, goals, competencies, milestones):
    for index, goal in enumerate(goals or []):
        DevelopmentPlanGoal.objects.create(
            organization=plan.organization,
            plan=plan,
            category=goal.get("category") or "",
            title=goal.get("title") or "",
            description=goal.get("description") or "",
            status=goal.get("status") or RecordStatus.ACTIVE,
            sort_order=goal.get("sort_order", index),
        )
    for index, competency in enumerate(competencies or []):
        DevelopmentPlanCompetency.objects.create(
            organization=plan.organization,
            plan=plan,
            name=competency.get("name") or "",
            competency_key=competency.get("competency_key") or "",
            domain=competency.get("domain") or "",
            pathways=competency.get("pathways") or [],
            description=competency.get("description") or "",
            target_level=competency.get("target_level") or "",
            current_level=competency.get("current_level") or "",
            sort_order=competency.get("sort_order", index),
        )
    for index, milestone in enumerate(milestones or []):
        DevelopmentPlanMilestone.objects.create(
            organization=plan.organization,
            plan=plan,
            title=milestone.get("title") or "",
            template_key=milestone.get("template_key") or "",
            criterion_count=milestone.get("criterion_count"),
            rationale=milestone.get("rationale") or "",
            status=milestone.get("status") or DevelopmentPlanMilestone.MilestoneStatus.ACTIVE,
            sort_order=milestone.get("sort_order", index),
        )


def snapshot_children(plan):
    return {
        "goals": [
            {
                "category": g.category,
                "title": g.title,
                "description": g.description,
                "status": g.status,
                "sort_order": g.sort_order,
            }
            for g in plan.goals.all()
        ],
        "competencies": [
            {
                "name": c.name,
                "competency_key": c.competency_key,
                "domain": c.domain,
                "pathways": c.pathways,
                "description": c.description,
                "target_level": c.target_level,
                "current_level": c.current_level,
                "sort_order": c.sort_order,
            }
            for c in plan.competencies.all()
        ],
        "milestones": [
            {
                "title": m.title,
                "template_key": m.template_key,
                "criterion_count": m.criterion_count,
                "rationale": m.rationale,
                "status": m.status,
                "sort_order": m.sort_order,
            }
            for m in plan.milestones.all()
        ],
    }


_GOAL_CATEGORIES = {value for value, _ in GOAL_CATEGORY_CHOICES}
_DOMAINS = {value for value, _ in DEVELOPMENT_DOMAIN_CHOICES}


def _tag_competency(competency, track):
    """Library competencies take their ID, domain and pathways from the library; a custom one keeps
    the supervisor's domain and is tagged with this relationship's pathway. Tags are saved on the row."""
    match = find_competency(competency.get("name"))
    if match:
        tags = {"competency_key": match["key"], "domain": match["domain"], "pathways": list(match["pathways"])}
    else:
        tags = {"competency_key": "", "domain": competency.get("domain") or "", "pathways": [track]}
    return {**competency, **tags}


def prepare_plan_items(*, relationship, goals=None, competencies=None, milestones=None):
    """Checks submitted items and returns the competencies with their tags.
    Items a revision copies over (lists left as None) aren't re-checked or re-tagged, so earlier
    versions keep what they were saved with (including free-text levels from before the fixed scale)."""
    for index, goal in enumerate(goals or [], start=1):
        if not (goal.get("title") or "").strip():
            raise ValidationError(f"Goal {index}: an individualized goal statement is required.")
        category = goal.get("category") or ""
        if category and category not in _GOAL_CATEGORIES:
            raise ValidationError(f"Goal {index}: unknown category '{category}'.")
    for index, competency in enumerate(competencies or [], start=1):
        current = competency.get("current_level") or ""
        target = competency.get("target_level") or ""
        domain = competency.get("domain") or ""
        if current and current not in CompetencyLevel.values:
            raise ValidationError(f"Competency {index}: unknown current level '{current}'.")
        if target and target not in TARGET_LEVELS:
            raise ValidationError(
                f"Competency {index}: target level must be Developing, Performs With Support, or Independent."
            )
        # Equal is allowed (maintenance, generalization, refinement); lower is not
        if current and target and COMPETENCY_LEVEL_ORDER.index(target) < COMPETENCY_LEVEL_ORDER.index(current):
            raise ValidationError(f"Competency {index}: target level can't be below the current level.")
        if domain and domain not in _DOMAINS:
            raise ValidationError(f"Competency {index}: unknown domain '{domain}'.")
    for index, milestone in enumerate(milestones or [], start=1):
        if not (milestone.get("title") or "").strip():
            raise ValidationError(f"Milestone {index}: the milestone text is required.")
        template_key = milestone.get("template_key") or ""
        if template_key and template_key not in MILESTONE_TEMPLATE_KEYS:
            raise ValidationError(f"Milestone {index}: unknown template '{template_key}'.")
        count = milestone.get("criterion_count")
        if count is not None and (isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 999):
            raise ValidationError(f"Milestone {index}: the number must be a whole number from 1 to 999.")
    if competencies is None:
        return None
    return [_tag_competency(competency, relationship.supervision_track) for competency in competencies]


def create_development_plan(
    *,
    organization,
    relationship,
    summary="",
    goals=None,
    competencies=None,
    milestones=None,
):
    existing = DevelopmentPlan.objects.filter(relationship=relationship, is_current=True).first()
    if existing:
        raise ValidationError(
            "A development plan already exists for this relationship. "
            "Edit the current plan to create a new version."
        )
    competencies = prepare_plan_items(
        relationship=relationship, goals=goals, competencies=competencies, milestones=milestones
    )
    with transaction.atomic():
        plan = DevelopmentPlan(
            organization=organization,
            relationship=relationship,
            plan_family_id=uuid.uuid4(),
            version_number=1,
            is_current=True,
            summary=summary or "",
        )
        plan.save()
        _create_children(plan, goals=goals, competencies=competencies, milestones=milestones)
    return (
        DevelopmentPlan.objects.select_related("relationship")
        .prefetch_related("goals", "competencies", "milestones")
        .get(pk=plan.pk)
    )


def revise_development_plan(current_plan, *, summary=None, goals=None, competencies=None, milestones=None):
    if not current_plan.is_current:
        raise PlanContentImmutable("Only the current development plan version can be edited.")
    competencies = prepare_plan_items(
        relationship=current_plan.relationship, goals=goals, competencies=competencies, milestones=milestones
    )
    snapshot = snapshot_children(current_plan)
    next_summary = current_plan.summary if summary is None else summary
    next_goals = snapshot["goals"] if goals is None else copy.deepcopy(goals)
    next_competencies = snapshot["competencies"] if competencies is None else copy.deepcopy(competencies)
    next_milestones = snapshot["milestones"] if milestones is None else copy.deepcopy(milestones)

    with transaction.atomic():
        current_plan.is_current = False
        current_plan.record_status = RecordStatus.SUPERSEDED
        current_plan.save(update_fields=["is_current", "record_status", "updated_at"])
        new_plan = DevelopmentPlan(
            organization=current_plan.organization,
            relationship=current_plan.relationship,
            plan_family_id=current_plan.plan_family_id,
            version_number=current_plan.version_number + 1,
            is_current=True,
            summary=next_summary or "",
        )
        new_plan.save()
        _create_children(
            new_plan,
            goals=next_goals,
            competencies=next_competencies,
            milestones=next_milestones,
        )
    return (
        DevelopmentPlan.objects.select_related("relationship")
        .prefetch_related("goals", "competencies", "milestones")
        .get(pk=new_plan.pk)
    )


def transition_milestone(milestone, *, status, rationale):
    if not milestone.plan.is_current:
        raise ValidationError("Cannot transition milestones on a superseded plan version.")
    if status not in DevelopmentPlanMilestone.TERMINAL_STATUSES:
        raise ValidationError("Milestone transition target must be Achieved or Discontinued.")
    if milestone.status != DevelopmentPlanMilestone.MilestoneStatus.ACTIVE:
        raise ValidationError("Only an Active milestone can be transitioned.")
    if not (rationale or "").strip():
        raise ValidationError(
            "Milestone transition from Active to Achieved or Discontinued requires a documented rationale."
        )
    milestone.status = status
    milestone.rationale = rationale.strip()
    milestone.save()
    return milestone
