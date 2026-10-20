from rest_framework import serializers
from drf_spectacular.types import OpenApiTypes

from apps.supervision.models import (
    DEVELOPMENT_DOMAIN_CHOICES,
    GOAL_CATEGORY_CHOICES,
    CompetencyLevel,
    CurrentContext,
    PriorAbaExperience,
)


JSON_RESPONSE = {200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT, 400: OpenApiTypes.OBJECT, 401: OpenApiTypes.OBJECT, 403: OpenApiTypes.OBJECT, 404: OpenApiTypes.OBJECT, 405: OpenApiTypes.OBJECT, 423: OpenApiTypes.OBJECT}


class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField()


class MFACodeSerializer(serializers.Serializer):
    code = serializers.CharField(help_text="6-digit authenticator code")


class WorkspaceSelectSerializer(serializers.Serializer):
    organization_id = serializers.UUIDField()


class RelationshipCreateSerializer(serializers.Serializer):
    supervisor_id = serializers.UUIDField()
    supervisee_id = serializers.UUIDField()
    supervision_track = serializers.CharField()
    fieldwork_subtype = serializers.CharField(required=False, allow_blank=True)
    supervisee_purpose = serializers.CharField(required=False, allow_blank=True)
    started_on = serializers.DateField()


class RelationshipPatchSerializer(serializers.Serializer):
    supervision_track = serializers.CharField(required=False)
    fieldwork_requirements = serializers.ChoiceField(choices=["by_month", "bacb_2022", "bacb_2027"], required=False, help_text="Supervised Fieldwork: which BACB standard months are measured against")
    prior_service_hours = serializers.DecimalField(max_digits=8, decimal_places=2, required=False, min_value=0, help_text="BCaBA: post-certification service hours before this record began")
    fieldwork_subtype = serializers.CharField(required=False, allow_blank=True)
    supervisee_purpose = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False)
    ended_on = serializers.DateField(required=False, allow_null=True)


class IntakeCreateSerializer(serializers.Serializer):
    captured_on = serializers.DateField()
    notes = serializers.CharField(required=False, allow_blank=True)
    prior_aba_experience = serializers.ChoiceField(choices=PriorAbaExperience.choices, required=False, allow_blank=True)
    prior_aba_experience_other = serializers.CharField(required=False, allow_blank=True, help_text="Required when prior_aba_experience is 'other'")
    current_context = serializers.ListField(child=serializers.ChoiceField(choices=CurrentContext.choices), required=False)
    current_context_other = serializers.CharField(required=False, allow_blank=True, help_text="Required when current_context includes 'other'")
    background = serializers.CharField(required=False, allow_blank=True)
    starting_domains = serializers.ListField(child=serializers.ChoiceField(choices=DEVELOPMENT_DOMAIN_CHOICES), required=False)
    starting_domains_other = serializers.CharField(required=False, allow_blank=True, help_text="Required when starting_domains includes 'other'")
    further_assessment_needed = serializers.BooleanField(required=False, allow_null=True, help_text="Leave out or null when not answered")
    starting_notes = serializers.CharField(required=False, allow_blank=True)


class PlanGoalSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=GOAL_CATEGORY_CHOICES, required=False, allow_blank=True)
    title = serializers.CharField(help_text="Individualized goal statement")
    description = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    sort_order = serializers.IntegerField(required=False)


class PlanCompetencySerializer(serializers.Serializer):
    name = serializers.CharField(help_text="A plan-library label is tagged with that entry's ID, domain and pathways; other text is a custom competency")
    domain = serializers.ChoiceField(choices=DEVELOPMENT_DOMAIN_CHOICES, required=False, allow_blank=True, help_text="Used for custom competencies only")
    description = serializers.CharField(required=False, allow_blank=True)
    target_level = serializers.ChoiceField(choices=CompetencyLevel.choices, required=False, allow_blank=True, help_text="Developing, Performs With Support or Independent; may equal current_level")
    current_level = serializers.ChoiceField(choices=CompetencyLevel.choices, required=False, allow_blank=True)
    sort_order = serializers.IntegerField(required=False)


class PlanMilestoneSerializer(serializers.Serializer):
    title = serializers.CharField(help_text="The milestone as written, including any edits to the template")
    template_key = serializers.CharField(required=False, allow_blank=True, help_text="plan-library milestone template; blank for a custom milestone")
    criterion_count = serializers.IntegerField(required=False, allow_null=True, min_value=1, max_value=999)
    rationale = serializers.CharField(required=False, allow_blank=True)
    status = serializers.CharField(required=False, allow_blank=True)
    sort_order = serializers.IntegerField(required=False)


class DevelopmentPlanWriteSerializer(serializers.Serializer):
    summary = serializers.CharField(required=False, allow_blank=True)
    goals = PlanGoalSerializer(many=True, required=False)
    competencies = PlanCompetencySerializer(many=True, required=False)
    milestones = PlanMilestoneSerializer(many=True, required=False)


class MilestoneTransitionSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["achieved", "discontinued"])
    rationale = serializers.CharField(required=False, allow_blank=True)


class InvitationCreateSerializer(serializers.Serializer):
    email = serializers.EmailField()
    credential_type = serializers.ChoiceField(
        choices=["", "bcba", "bcaba", "rbt", "student_analyst"], required=False
    )
    org_role = serializers.ChoiceField(
        choices=["", "administrator", "clinical_director"], required=False
    )


class InvitationVerifySerializer(serializers.Serializer):
    full_name = serializers.CharField()
    password = serializers.CharField()


class InvitationActivateSerializer(InvitationVerifySerializer):
    code = serializers.CharField(help_text="6-digit authenticator code")


class PasswordForgotSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordCodeSerializer(PasswordForgotSerializer):
    code = serializers.CharField(max_length=6)


class PasswordResetSerializer(PasswordCodeSerializer):
    password = serializers.CharField(write_only=True)


# ---------------------------------------------------------------------------
# Monthly Work & Hours
# ---------------------------------------------------------------------------

class CycleCreateSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    fieldwork_type = serializers.ChoiceField(
        choices=["supervised", "concentrated"], required=False, allow_blank=True,
        help_text="Supervised Fieldwork only; defaults to the relationship's subtype",
    )


class CyclePatchSerializer(serializers.Serializer):
    fieldwork_type = serializers.ChoiceField(choices=["supervised", "concentrated"])


class AssignmentCreateSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True)
    instructions = serializers.CharField(required=False, allow_blank=True)
    assigned_on = serializers.DateField(required=False, allow_null=True)
    due_on = serializers.DateField(required=False, allow_null=True)
    year = serializers.IntegerField(required=False, min_value=2000, max_value=2100, help_text="Cycle year; defaults to the due date's month")
    month = serializers.IntegerField(required=False, min_value=1, max_value=12)
    related_goal_id = serializers.UUIDField(required=False, allow_null=True, help_text="A goal from this relationship's development plan")
    related_competency_id = serializers.UUIDField(required=False, allow_null=True)
    requires_written_response = serializers.BooleanField(required=False, default=False)
    requires_file = serializers.BooleanField(required=False, default=False)
    requires_evidence = serializers.BooleanField(required=False, default=False)
    draft = serializers.BooleanField(required=False, default=False, help_text="Save without issuing; the Supervisee can't see drafts")


class AssignmentPatchSerializer(AssignmentCreateSerializer):
    """Editing a draft; every field optional."""

    title = serializers.CharField(max_length=255, required=False)
    requires_written_response = serializers.BooleanField(required=False)
    requires_file = serializers.BooleanField(required=False)
    requires_evidence = serializers.BooleanField(required=False)
    draft = None


class AssignmentSubmitSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)
    files = serializers.ListField(child=serializers.FileField(), required=False, help_text="Up to 5 files, multipart/form-data")


class AssignmentReviewSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, help_text="Required when requesting a revision")


class HoursCreateSerializer(serializers.Serializer):
    occurred_on = serializers.DateField()
    hours = serializers.DecimalField(max_digits=6, decimal_places=2)
    kind = serializers.ChoiceField(choices=["independent", "supervision"])
    activity_category = serializers.ChoiceField(choices=["unrestricted", "restricted"], required=False, default="unrestricted", help_text="BACB: at least 60% of all fieldwork hours must be unrestricted")
    supervision_format = serializers.ChoiceField(choices=["individual", "group"], required=False, allow_blank=True)
    client_observation = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class HoursPatchSerializer(serializers.Serializer):
    occurred_on = serializers.DateField(required=False)
    hours = serializers.DecimalField(max_digits=6, decimal_places=2, required=False)
    kind = serializers.ChoiceField(choices=["independent", "supervision"], required=False)
    activity_category = serializers.ChoiceField(choices=["unrestricted", "restricted"], required=False)
    supervision_format = serializers.ChoiceField(choices=["individual", "group"], required=False, allow_blank=True)
    client_observation = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class HoursReturnSerializer(serializers.Serializer):
    reason = serializers.CharField()


class SessionCreateSerializer(serializers.Serializer):
    occurred_on = serializers.DateField()
    start_time = serializers.TimeField(required=False, allow_null=True)
    end_time = serializers.TimeField(required=False, allow_null=True)
    duration_minutes = serializers.IntegerField(min_value=1, max_value=1440, required=False, help_text="Not needed when start and end times are given")
    session_type = serializers.ChoiceField(choices=["individual", "group"])
    meeting_method = serializers.ChoiceField(choices=["in_person", "remote", "phone", "asynchronous"], required=False, allow_blank=True, help_text="Phone and asynchronous records never count toward requirements")
    client_observation = serializers.BooleanField(required=False, default=False)
    appointment_id = serializers.UUIDField(required=False, allow_null=True, help_text="The appointment this record documents")
    topics = serializers.CharField(required=False, allow_blank=True)
    activities = serializers.CharField(required=False, allow_blank=True)
    feedback = serializers.CharField(required=False, allow_blank=True)
    follow_up = serializers.CharField(required=False, allow_blank=True)
    notes = serializers.CharField(required=False, allow_blank=True)
    files = serializers.ListField(child=serializers.FileField(), required=False, help_text="Up to 5 files, multipart/form-data")
    draft = serializers.BooleanField(required=False, default=False, help_text="Save without completing; drafts never count")


class SessionPatchSerializer(SessionCreateSerializer):
    """Editing a draft; every field optional. `complete` turns it into a completed record."""

    occurred_on = serializers.DateField(required=False)
    session_type = serializers.ChoiceField(choices=["individual", "group"], required=False)
    client_observation = serializers.BooleanField(required=False)
    draft = None
    complete = serializers.BooleanField(required=False, default=False)


class NoteCreateSerializer(serializers.Serializer):
    body = serializers.CharField()


class ServiceHoursSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    hours = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=0)
    zero_reason = serializers.ChoiceField(
        choices=["no_services_scheduled", "client_unavailable", "illness", "service_hold", "supervisee_absence", "other"],
        required=False, allow_blank=True, help_text="Required when hours is 0 (a confirmed zero, not missing)",
    )
    notes = serializers.CharField(required=False, allow_blank=True)
    supporting_reference = serializers.CharField(required=False, allow_blank=True, max_length=255)


class CycleStatusSerializer(serializers.Serializer):
    status = serializers.ChoiceField(choices=["open", "ready_for_review", "closed"])
    reason = serializers.CharField(required=False, allow_blank=True, help_text="Required when reopening a closed month")


class CoordinatorSerializer(serializers.Serializer):
    coordinator_id = serializers.UUIDField()
    starts_on = serializers.DateField()
    ends_on = serializers.DateField(required=False, allow_null=True)


class CertificationSerializer(serializers.Serializer):
    certification_number = serializers.CharField(allow_blank=True, max_length=40)


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------

class AppointmentCreateSerializer(serializers.Serializer):
    starts_at = serializers.DateTimeField()
    ends_at = serializers.DateTimeField()
    session_type = serializers.ChoiceField(choices=["individual", "group"], required=False, default="individual")
    meeting_method = serializers.ChoiceField(choices=["in_person", "remote", "phone", "asynchronous"], required=False, allow_blank=True)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    agenda = serializers.CharField(required=False, allow_blank=True)
    repeat_weeks = serializers.IntegerField(required=False, default=0, min_value=0, max_value=25, help_text="Batch: this many more weekly appointments after the first")


class AppointmentPatchSerializer(serializers.Serializer):
    starts_at = serializers.DateTimeField(required=False)
    ends_at = serializers.DateTimeField(required=False)
    session_type = serializers.ChoiceField(choices=["individual", "group"], required=False)
    meeting_method = serializers.ChoiceField(choices=["in_person", "remote", "phone", "asynchronous"], required=False, allow_blank=True)
    location = serializers.CharField(required=False, allow_blank=True, max_length=255)
    agenda = serializers.CharField(required=False, allow_blank=True)
    reason = serializers.CharField(required=False, allow_blank=True, help_text="Why it was rescheduled")


class AppointmentCancelSerializer(serializers.Serializer):
    reason = serializers.CharField()


class PrepNoteSerializer(serializers.Serializer):
    body = serializers.CharField()


class AssessmentWriteSerializer(serializers.Serializer):
    plan_competency_id = serializers.UUIDField(required=False, help_text="A competency from this relationship's development plan")
    related_goal_id = serializers.UUIDField(required=False, allow_null=True)
    assessed_on = serializers.DateField(required=False)
    rating = serializers.ChoiceField(choices=[c for c in CompetencyLevel.values if c != "not_yet_assessed"], required=False, allow_blank=True)
    documented_content = serializers.CharField(required=False, allow_blank=True, help_text="Evidence and observations behind the rating")
