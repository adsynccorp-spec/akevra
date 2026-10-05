"""Suggested competencies and milestone templates offered when writing a development plan.

Suggestions only: nothing here is applied to a plan automatically, and milestone thresholds are
clinical examples, not BACB requirements.

Keys are stable IDs saved on plan items. Plan items also save their own text, domain and pathway
tags, so editing this library never changes an earlier plan version. Never reuse or rename a key;
to retire an entry, set "retired": True so it is no longer suggested.
"""

from apps.supervision.models import SupervisionTrack

ALL_TRACKS = [value for value, _ in SupervisionTrack.choices]
# Higher-level options for analyst-level pathways (everything except RBT ongoing supervision)
ANALYST_TRACKS = [track for track in ALL_TRACKS if track != SupervisionTrack.RBT_ONGOING]


def _competency(key, label, domain, pathways):
    return {"key": key, "label": label, "domain": domain, "pathways": list(pathways)}


COMPETENCIES = [
    # Implementation level: suggested on every pathway
    _competency("accurate_data_collection", "Accurate data collection", "data_collection", ALL_TRACKS),
    _competency("objective_session_documentation", "Objective session documentation", "clinical_documentation", ALL_TRACKS),
    _competency("timely_documentation", "Timely documentation", "clinical_documentation", ALL_TRACKS),
    _competency("session_preparation", "Session preparation", "session_preparation", ALL_TRACKS),
    _competency("identifying_antecedents_consequences", "Identifying antecedents and consequences", "behavior_reduction", ALL_TRACKS),
    _competency("implementing_reinforcement", "Implementing reinforcement procedures", "reinforcement", ALL_TRACKS),
    _competency("implementing_prompting", "Implementing prompting procedures", "prompting", ALL_TRACKS),
    _competency("prompt_fading", "Prompt fading", "prompting", ALL_TRACKS),
    _competency("implementing_skill_acquisition", "Implementing skill-acquisition procedures", "skill_acquisition", ALL_TRACKS),
    _competency("implementing_behavior_reduction", "Implementing behavior-reduction procedures", "behavior_reduction", ALL_TRACKS),
    _competency("implementing_functional_communication", "Implementing functional communication procedures", "functional_communication", ALL_TRACKS),
    _competency("preference_assessments", "Conducting preference/reinforcer assessments when appropriate to role", "reinforcement", ALL_TRACKS),
    _competency("maintaining_treatment_integrity", "Maintaining treatment integrity", "treatment_integrity", ALL_TRACKS),
    _competency("supporting_generalization", "Supporting generalization and maintenance", "generalization_maintenance", ALL_TRACKS),
    _competency("professional_communication", "Professional communication", "professional_communication", ALL_TRACKS),
    _competency("caregiver_staff_interaction", "Caregiver/staff interaction within role", "stakeholder_collaboration", ALL_TRACKS),
    _competency("responding_to_feedback", "Responding appropriately to clinical feedback", "professional_communication", ALL_TRACKS),
    _competency("ethical_decision_making", "Ethical decision-making", "ethics", ALL_TRACKS),
    _competency("professional_boundaries", "Maintaining professional boundaries", "ethics", ALL_TRACKS),
    # Analyst level: BCaBA ongoing supervision, supervised fieldwork, BCBA professional development
    _competency("interpreting_clinical_data", "Interpreting clinical data", "data_collection", ANALYST_TRACKS),
    _competency("data_based_recommendations", "Making data-based recommendations", "clinical_reasoning", ANALYST_TRACKS),
    _competency("developing_skill_acquisition", "Developing skill-acquisition programming", "skill_acquisition", ANALYST_TRACKS),
    _competency("developing_behavior_reduction", "Developing behavior-reduction programming", "behavior_reduction", ANALYST_TRACKS),
    _competency("conducting_assessments", "Conducting assessment activities appropriate to role and supervision", "clinical_reasoning", ANALYST_TRACKS),
    _competency("evaluating_treatment_integrity", "Evaluating treatment integrity", "treatment_integrity", ANALYST_TRACKS),
    _competency("evaluating_treatment_effectiveness", "Evaluating treatment effectiveness", "clinical_reasoning", ANALYST_TRACKS),
    _competency("modifying_programming", "Modifying programming based on data", "clinical_reasoning", ANALYST_TRACKS),
    _competency("treatment_plan_development", "Clinical documentation and treatment-plan development", "clinical_documentation", ANALYST_TRACKS),
    _competency("stakeholder_training", "Caregiver/stakeholder training", "stakeholder_collaboration", ANALYST_TRACKS),
    _competency("case_conceptualization", "Clinical reasoning and case conceptualization", "clinical_reasoning", ANALYST_TRACKS),
    _competency("supervision_delegation", "Supervision/delegation competencies when applicable", "professional_communication", ANALYST_TRACKS),
]

# "[#]" is where the supervisor enters the number; the whole text stays editable
MILESTONE_TEMPLATES = [
    {"key": "consecutive_observations", "text": "Demonstrates the competency across [#] consecutive observations."},
    {"key": "documentation_reviews", "text": "Completes [#] consecutive documentation reviews meeting established criteria."},
    {"key": "independent_implementation", "text": "Implements the procedure independently across [#] consecutive supervision observations."},
    {"key": "clients_settings", "text": "Demonstrates the skill across [#] relevant clients/settings when appropriate."},
    {"key": "individualized_criterion", "text": "Meets the individualized performance criterion across [#] consecutive opportunities/observations."},
    {"key": "support_level", "text": "Demonstrates the competency with no more than the specified level of supervisor support across [#] observations."},
]

COMPETENCIES_BY_KEY = {item["key"]: item for item in COMPETENCIES}
MILESTONE_TEMPLATE_KEYS = frozenset(item["key"] for item in MILESTONE_TEMPLATES)


def find_competency(name):
    """The library entry whose label matches `name` exactly (ignoring case and spacing), else None."""
    wanted = " ".join((name or "").split()).lower()
    return next((item for item in COMPETENCIES if item["label"].lower() == wanted), None)


def serialize_library(track=None):
    competencies = [item for item in COMPETENCIES if not item.get("retired")]
    if track:
        competencies = [item for item in competencies if track in item["pathways"]]
    return {
        "competencies": competencies,
        "milestone_templates": [item for item in MILESTONE_TEMPLATES if not item.get("retired")],
    }
