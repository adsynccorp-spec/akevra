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
    instructions = serializers.CharField(required=False, allow_blank=True)
    due_on = serializers.DateField(required=False, allow_null=True)
    year = serializers.IntegerField(required=False, min_value=2000, max_value=2100, help_text="Cycle year; defaults to the due date's month")
    month = serializers.IntegerField(required=False, min_value=1, max_value=12)


class AssignmentSubmitSerializer(serializers.Serializer):
    note = serializers.CharField(required=False, allow_blank=True)
    files = serializers.ListField(child=serializers.FileField(), required=False, help_text="Up to 5 files, multipart/form-data")


class AssignmentReviewSerializer(serializers.Serializer):
    comment = serializers.CharField(required=False, allow_blank=True, help_text="Required when requesting a revision")


class HoursCreateSerializer(serializers.Serializer):
    occurred_on = serializers.DateField()
    hours = serializers.DecimalField(max_digits=6, decimal_places=2)
    kind = serializers.ChoiceField(choices=["independent", "supervision"])
    supervision_format = serializers.ChoiceField(choices=["individual", "group"], required=False, allow_blank=True)
    client_observation = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class HoursPatchSerializer(serializers.Serializer):
    occurred_on = serializers.DateField(required=False)
    hours = serializers.DecimalField(max_digits=6, decimal_places=2, required=False)
    kind = serializers.ChoiceField(choices=["independent", "supervision"], required=False)
    supervision_format = serializers.ChoiceField(choices=["individual", "group"], required=False, allow_blank=True)
    client_observation = serializers.BooleanField(required=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class HoursReturnSerializer(serializers.Serializer):
    reason = serializers.CharField()


class SessionCreateSerializer(serializers.Serializer):
    occurred_on = serializers.DateField()
    duration_minutes = serializers.IntegerField(min_value=1, max_value=1440)
    session_type = serializers.ChoiceField(choices=["individual", "group"])
    client_observation = serializers.BooleanField(required=False, default=False)
    notes = serializers.CharField(required=False, allow_blank=True)


class ServiceHoursSerializer(serializers.Serializer):
    year = serializers.IntegerField(min_value=2000, max_value=2100)
    month = serializers.IntegerField(min_value=1, max_value=12)
    hours = serializers.DecimalField(max_digits=7, decimal_places=2, min_value=0)
    notes = serializers.CharField(required=False, allow_blank=True)
