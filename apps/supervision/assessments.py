"""Competency assessments: Draft -> Ready for Review -> Official. Official records are final."""

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.supervision.models import (
    AssessmentStatus,
    CompetencyAssessment,
    CompetencyLevel,
    DevelopmentPlanCompetency,
    DevelopmentPlanGoal,
)

RATINGS = [value for value in CompetencyLevel.values if value != CompetencyLevel.NOT_YET_ASSESSED]
FIELDS = ("plan_competency_id", "related_goal_id", "assessed_on", "rating", "documented_content")


def _apply(assessment, relationship, fields):
    for key, value in fields.items():
        if key in FIELDS and value is not None:
            setattr(assessment, key, value)
    if assessment.rating and assessment.rating not in RATINGS:
        raise ValidationError("Choose a rating from the competency scale.")
    if not assessment.plan_competency_id:
        raise ValidationError("Choose the competency being assessed.")
    competency = DevelopmentPlanCompetency.objects.filter(
        pk=assessment.plan_competency_id, plan__relationship=relationship
    ).first()
    if competency is None:
        raise ValidationError("That competency isn't part of this relationship's development plan.")
    assessment.competency_name = competency.name
    if assessment.related_goal_id and not DevelopmentPlanGoal.objects.filter(
        pk=assessment.related_goal_id, plan__relationship=relationship
    ).exists():
        raise ValidationError("That goal isn't part of this relationship's development plan.")
    if not assessment.assessed_on:
        assessment.assessed_on = timezone.localdate()


def create_assessment(relationship, actor, **fields):
    assessment = CompetencyAssessment(
        organization=relationship.organization, relationship=relationship, assessed_by=actor,
        status=AssessmentStatus.DRAFT,
    )
    _apply(assessment, relationship, fields)
    assessment.save()
    return assessment


def update_assessment(assessment, **fields):
    if assessment.status == AssessmentStatus.OFFICIAL:
        raise ValidationError("An official assessment is final. Record a new assessment instead.")
    _apply(assessment, assessment.relationship, fields)
    assessment.save()
    return assessment


def submit_for_review(assessment):
    if assessment.status != AssessmentStatus.DRAFT:
        raise ValidationError("Only a draft can be sent for review.")
    if not assessment.rating:
        raise ValidationError("Give a rating before sending the assessment for review.")
    assessment.status = AssessmentStatus.READY_FOR_REVIEW
    assessment.save()
    return assessment


def return_to_draft(assessment):
    if assessment.status != AssessmentStatus.READY_FOR_REVIEW:
        raise ValidationError("Only an assessment waiting for review can be returned to draft.")
    assessment.status = AssessmentStatus.DRAFT
    assessment.save()
    return assessment


def finalize(assessment):
    if assessment.status == AssessmentStatus.OFFICIAL:
        raise ValidationError("This assessment is already official.")
    if not assessment.rating:
        raise ValidationError("Give a rating before finalizing.")
    if not (assessment.documented_content or "").strip():
        raise ValidationError("Document the evidence for the rating before finalizing.")
    assessment.status = AssessmentStatus.OFFICIAL
    assessment.approved = True
    assessment.finalized_at = timezone.now()
    assessment.save()
    return assessment


def serialize_assessment(a):
    return {
        "id": str(a.id),
        "relationship_id": str(a.relationship_id),
        "competency": {
            "id": str(a.plan_competency_id) if a.plan_competency_id else None,
            "name": a.competency_name or (a.plan_competency.name if a.plan_competency_id else ""),
            "description": a.plan_competency.description if a.plan_competency_id else "",
            "domain": a.plan_competency.domain if a.plan_competency_id else "",
        },
        "related_goal": None if not a.related_goal_id else {
            "id": str(a.related_goal_id), "title": a.related_goal.title, "category": a.related_goal.category,
        },
        "assessed_on": a.assessed_on.isoformat(),
        "rating": a.rating,
        "documented_content": a.documented_content,
        "status": a.status,
        "assessed_by": None if a.assessed_by_id is None else {"id": str(a.assessed_by_id), "name": a.assessed_by.full_name},
        "finalized_at": a.finalized_at.isoformat() if a.finalized_at else None,
        "updated_at": a.updated_at.isoformat(),
    }


def summarize(assessments):
    """Totals from each competency's latest official assessment."""
    latest = {}
    for a in sorted(assessments, key=lambda x: (x.assessed_on, x.created_at)):
        if a.status == AssessmentStatus.OFFICIAL:
            latest[a.competency_name.lower()] = a.rating
    ratings = list(latest.values())
    return {
        "total_assessed": len(ratings),
        "proficient": sum(r in (CompetencyLevel.INDEPENDENT, CompetencyLevel.PERFORMS_WITH_SUPPORT) for r in ratings),
        "developing": sum(r == CompetencyLevel.DEVELOPING for r in ratings),
        "needs_improvement": sum(r == CompetencyLevel.NEEDS_TRAINING for r in ratings),
    }
