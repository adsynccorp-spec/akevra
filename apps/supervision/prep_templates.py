"""Supervision Preparation Form templates, one per pathway.

The structure (sections and questions) is data, not code: replacing a question's wording or adding
one means editing this file only, and every saved form keeps the template key it was answered on.
The client's own Google Form questions slot in here once shared (the links require sign-in).
"""

COMMON_CLOSING = [
    {"key": "questions_for_supervisor", "label": "What questions do you want your supervisor to answer in this session?", "type": "textarea", "required": True},
    {"key": "support_needed", "label": "What support, modeling or feedback would help you most right now?", "type": "textarea"},
    {"key": "confidence", "label": "How confident do you feel about your current cases? (1 = not confident, 5 = very confident)", "type": "scale", "min": 1, "max": 5},
]

TEMPLATES = {
    "rbt": {
        "title": "RBT Supervision Preparation",
        "sections": [
            {"title": "Since your last supervision", "questions": [
                {"key": "wins", "label": "What went well with your clients since your last supervision?", "type": "textarea", "required": True},
                {"key": "challenges", "label": "What challenges did you run into (behaviors, programs, data collection)?", "type": "textarea", "required": True},
                {"key": "clients_to_discuss", "label": "Which client(s) or programs do you want to discuss?", "type": "textarea"},
            ]},
            {"title": "Practice and data", "questions": [
                {"key": "data_concerns", "label": "Any data collection, graphing or session-note questions?", "type": "textarea"},
                {"key": "skills_to_practice", "label": "Which skills from the RBT Task List would you like to practice or be observed on?", "type": "textarea"},
                {"key": "ethics_concerns", "label": "Any ethical, safety or professional concerns to raise?", "type": "select",
                 "options": ["No", "Yes — I'll describe below"]},
                # Shown, and then required, only when the concern question is answered Yes
                {"key": "ethics_details", "label": "If yes, describe the concern", "type": "textarea", "required": True,
                 "show_if": {"key": "ethics_concerns", "equals": "Yes — I'll describe below"}},
            ]},
            {"title": "For this session", "questions": COMMON_CLOSING},
        ],
    },
    "bcaba": {
        "title": "BCaBA Supervision Preparation",
        "sections": [
            {"title": "Caseload update", "questions": [
                {"key": "caseload_update", "label": "Summarize your caseload and any changes since your last supervision.", "type": "textarea", "required": True},
                {"key": "progress_concerns", "label": "Which clients are not progressing as expected, and what have you tried?", "type": "textarea", "required": True},
                {"key": "plans_to_review", "label": "Behavior plans, skill-acquisition programs or assessments you want reviewed", "type": "textarea"},
            ]},
            {"title": "Supervision of others and professional practice", "questions": [
                {"key": "rbt_supervision", "label": "Any RBT supervision or training issues to discuss?", "type": "textarea"},
                {"key": "ethics_concerns", "label": "Ethical or professional concerns (Ethics Code for Behavior Analysts)", "type": "textarea"},
                {"key": "development_goals", "label": "Which development-plan goal do you want to work on in this session?", "type": "textarea"},
            ]},
            {"title": "For this session", "questions": COMMON_CLOSING},
        ],
    },
    "bcba": {
        "title": "BCBA Consultation / Professional Development Preparation",
        "sections": [
            {"title": "Focus", "questions": [
                {"key": "topic", "label": "What topic, case or competency do you want to focus on?", "type": "textarea", "required": True},
                {"key": "context", "label": "Relevant background or what you have tried so far", "type": "textarea"},
                {"key": "desired_outcome", "label": "What would a successful session look like for you?", "type": "textarea", "required": True},
            ]},
            {"title": "Development", "questions": [
                {"key": "competency_goal", "label": "Which professional-development goal does this relate to?", "type": "textarea"},
                {"key": "resources", "label": "Materials you will bring or want reviewed (plans, data, articles)", "type": "textarea"},
            ]},
            {"title": "For this session", "questions": COMMON_CLOSING[:2]},
        ],
    },
    "student": {
        "title": "Student Analyst (Fieldwork) Supervision Preparation",
        "sections": [
            {"title": "Fieldwork since last supervision", "questions": [
                {"key": "activities", "label": "Summarize your fieldwork activities since the last supervision (restricted and unrestricted).", "type": "textarea", "required": True},
                {"key": "task_list_items", "label": "Which BCBA Test Content Outline areas did you work on?", "type": "textarea"},
                {"key": "products", "label": "Work products to review (assessments, plans, graphs, write-ups)", "type": "textarea"},
            ]},
            {"title": "Learning", "questions": [
                {"key": "challenges", "label": "What was difficult, and what do you want feedback on?", "type": "textarea", "required": True},
                {"key": "observation_request", "label": "Would you like to be observed with a client this period?", "type": "select", "options": ["No", "Yes"]},
                {"key": "ethics_concerns", "label": "Any ethical or professional questions?", "type": "textarea"},
            ]},
            {"title": "For this session", "questions": COMMON_CLOSING},
        ],
    },
}

TRACK_TEMPLATE = {
    "rbt_ongoing_supervision": "rbt",
    "bcaba_ongoing_supervision": "bcaba",
    "bcba_professional_development": "bcba",
    "supervised_fieldwork": "student",
}


def template_for(track):
    key = TRACK_TEMPLATE.get(track, "rbt")
    return key, TEMPLATES[key]


def questions(template):
    return [q for section in template["sections"] for q in section["questions"]]


def is_shown(question, answers):
    """A conditional question applies only when the answer it depends on matches."""
    condition = question.get("show_if")
    return not condition or (answers or {}).get(condition["key"]) == condition["equals"]


def validate_answers(template, answers):
    """Required questions must be answered; unknown keys and answers to hidden conditional questions
    are dropped. Returns the cleaned answers."""
    from django.core.exceptions import ValidationError

    known = {q["key"]: q for q in questions(template)}
    cleaned = {k: (str(v).strip() if v is not None else "") for k, v in (answers or {}).items() if k in known}
    cleaned = {k: v for k, v in cleaned.items() if is_shown(known[k], cleaned)}
    missing = [q["label"] for k, q in known.items() if q.get("required") and is_shown(q, cleaned) and not cleaned.get(k)]
    if missing:
        raise ValidationError("Please answer: " + "; ".join(missing))
    return cleaned


def summary(template, answers):
    """A readable text version, kept in `body` for lists, emails and older views."""
    lines = []
    for q in questions(template):
        value = answers.get(q["key"])
        if value:
            lines.append(f"{q['label']}\n{value}")
    return "\n\n".join(lines)
