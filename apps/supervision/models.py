from django.core.exceptions import ValidationError
from django.db import models

from apps.accounts.models import CredentialType
from apps.core.models import (
    OrganizationScopedModel,
    RecordStatus,
    SoftArchiveModel,
    TimestampedModel,
    UUIDPrimaryKeyModel,
)

COMPLIANCE_BEARING_TRACKS = frozenset(
    {
        "rbt_ongoing_supervision",
        "supervised_fieldwork",
        "bcaba_ongoing_supervision",
    }
)


class SupervisionTrack(models.TextChoices):
    RBT_ONGOING = "rbt_ongoing_supervision", "RBT Ongoing Supervision"
    SUPERVISED_FIELDWORK = "supervised_fieldwork", "Supervised Fieldwork"
    BCABA_ONGOING = "bcaba_ongoing_supervision", "BCaBA Ongoing Supervision"
    BCBA_PROFESSIONAL_DEVELOPMENT = (
        "bcba_professional_development",
        "BCBA Professional Development",
    )


# Pathways each supervisee credential may use; the profile credential is the source of truth.
# RBTs and BCaBAs may also accrue Supervised Fieldwork toward a higher certification.
ELIGIBLE_TRACKS = {
    CredentialType.BCBA: [SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT],
    CredentialType.BCABA: [SupervisionTrack.BCABA_ONGOING, SupervisionTrack.SUPERVISED_FIELDWORK],
    CredentialType.RBT: [SupervisionTrack.RBT_ONGOING, SupervisionTrack.SUPERVISED_FIELDWORK],
    CredentialType.STUDENT_ANALYST: [SupervisionTrack.SUPERVISED_FIELDWORK],
    "": [SupervisionTrack.SUPERVISED_FIELDWORK],
}


def eligible_tracks(credential_type):
    return ELIGIBLE_TRACKS.get(credential_type or "", [])


class FieldworkSubtype(models.TextChoices):
    SUPERVISED = "supervised", "Supervised"
    CONCENTRATED = "concentrated", "Concentrated"


class SuperviseeAssignmentPurpose(models.TextChoices):
    """DEC-058 reasons a BCBA may be assigned as Supervisee. Not a fifth role."""

    STRUCTURED_PROFESSIONAL_DEVELOPMENT = (
        "structured_professional_development",
        "Structured professional development",
    )
    CONSULTATION = "consultation", "Consultation"
    COMPETENCY_DEVELOPMENT = "competency_development", "Competency development"
    LEADERSHIP_DEVELOPMENT = "leadership_development", "Leadership development"
    CASE_DISCUSSION = "case_discussion", "Case discussion"


class PriorAbaExperience(models.TextChoices):
    NONE = "none", "No prior ABA experience"
    SOME = "some", "Some prior ABA experience"
    RBT = "rbt", "Prior RBT experience"
    BCABA = "bcaba", "Prior BCaBA experience"
    OTHER = "other", "Other ABA experience"
    NOT_DETERMINED = "not_determined", "Not yet determined"


# Pathway is deliberately absent: the relationship's supervision_track already records it.
class CurrentContext(models.TextChoices):
    NEW_ROLE = "new_role", "New to current role"
    NEW_ORGANIZATION = "new_organization", "New to current organization"
    NEW_CLIENT_POPULATION = "new_client_population", "New to current client/population"
    NEW_SERVICE_SETTING = "new_service_setting", "New to current service setting"
    TRANSITIONING_SUPERVISOR = "transitioning_supervisor", "Transitioning from another supervisor"
    PRIOR_FORMAL_SUPERVISION = "prior_formal_supervision", "Prior experience receiving formal ABA supervision"
    OTHER = "other", "Other"


# Shared by the intake starting point and development goal categories so the two can be reported together
_DOMAINS = [
    ("data_collection", "Data Collection & Measurement"),
    ("clinical_documentation", "Clinical Documentation"),
    ("skill_acquisition", "Skill Acquisition"),
    ("behavior_reduction", "Behavior Reduction"),
    ("reinforcement", "Reinforcement"),
    ("prompting", "Prompting & Prompt Fading"),
    ("functional_communication", "Functional Communication"),
    ("generalization_maintenance", "Generalization & Maintenance"),
    ("treatment_integrity", "Treatment Integrity"),
    ("professional_communication", "Professional Communication"),
    ("stakeholder_collaboration", "Caregiver/Stakeholder Collaboration"),
    ("ethics", "Ethics & Professional Conduct"),
    ("clinical_reasoning", "Clinical Reasoning"),
    ("session_preparation", "Session Preparation & Organization"),
]
DEVELOPMENT_DOMAIN_CHOICES = _DOMAINS + [("other", "Other")]
GOAL_CATEGORY_CHOICES = _DOMAINS + [("professional_growth", "Professional Growth"), ("other", "Other")]


class CompetencyLevel(models.TextChoices):
    NOT_YET_ASSESSED = "not_yet_assessed", "Not Yet Assessed"
    NEEDS_TRAINING = "needs_training", "Needs Training"
    DEVELOPING = "developing", "Developing"
    PERFORMS_WITH_SUPPORT = "performs_with_support", "Performs With Support"
    INDEPENDENT = "independent", "Independent"


# Ordered lowest to highest; a target may equal the current level (maintenance, generalization, refinement)
COMPETENCY_LEVEL_ORDER = [value for value, _ in CompetencyLevel.choices]
TARGET_LEVELS = frozenset(
    {CompetencyLevel.DEVELOPING, CompetencyLevel.PERFORMS_WITH_SUPPORT, CompetencyLevel.INDEPENDENT}
)


def _require_choices(values, choices, label):
    allowed = {value for value, _ in choices}
    unknown = [value for value in values if value not in allowed]
    if unknown:
        raise ValidationError(f"Unknown {label}: {', '.join(map(str, unknown))}.")


class SupervisoryRelationship(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Center of the Version 2.3 data model. Roles are evaluated per row (DEC-057)."""

    supervisor = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="supervised_relationships",
    )
    supervisee = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="supervisee_relationships",
    )
    supervision_track = models.CharField(max_length=64, choices=SupervisionTrack.choices)
    fieldwork_subtype = models.CharField(
        max_length=32,
        choices=FieldworkSubtype.choices,
        blank=True,
        default="",
    )
    supervisee_purpose = models.CharField(
        max_length=64,
        choices=SuperviseeAssignmentPurpose.choices,
        blank=True,
        default="",
    )
    started_on = models.DateField()
    ended_on = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=32, default=RecordStatus.ACTIVE)

    class Meta:
        db_table = "supervisory_relationship"
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(supervisor=models.F("supervisee")),
                name="relationship_supervisor_ne_supervisee",
            ),
        ]

    @property
    def is_compliance_bearing(self):
        """DEC-058: assigning a BCBA as Supervisee does not itself make this compliance-bearing."""
        if self.supervision_track == SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT:
            return False
        return self.supervision_track in COMPLIANCE_BEARING_TRACKS

    def clean(self):
        if self.supervisor_id == self.supervisee_id:
            raise ValidationError("Supervisor and supervisee must be different people.")
        if self.supervisor.organization_id != self.organization_id:
            raise ValidationError("Supervisor must belong to the same organization.")
        if self.supervisee.organization_id != self.organization_id:
            raise ValidationError("Supervisee must belong to the same organization.")
        if self.supervisor.credential_type != CredentialType.BCBA:
            raise ValidationError(
                "Version 2.3 role model: Supervisor must be a BCBA. "
                "BCaBAs and other credentials cannot be assigned as independent Supervisors."
            )
        if (
            self.supervision_track == SupervisionTrack.SUPERVISED_FIELDWORK
            and not self.fieldwork_subtype
        ):
            raise ValidationError("Supervised Fieldwork requires a subtype.")
        self._clean_dec_058()
        self._clean_pathway_eligibility()

    def _clean_pathway_eligibility(self):
        allowed = eligible_tracks(self.supervisee.credential_type)
        if self.supervision_track not in allowed:
            credential = CredentialType(self.supervisee.credential_type).label if self.supervisee.credential_type else "no credential"
            names = " or ".join(SupervisionTrack(track).label for track in allowed)
            raise ValidationError(
                f"A supervisee with {credential} on their profile can only use {names}."
            )

    def _clean_dec_058(self):
        supervisee_is_bcba = self.supervisee.credential_type == CredentialType.BCBA
        if supervisee_is_bcba:
            if self.supervision_track != SupervisionTrack.BCBA_PROFESSIONAL_DEVELOPMENT:
                raise ValidationError(
                    "A BCBA assigned as Supervisee must use the BCBA Professional Development "
                    "track (DEC-058). This adds no fifth role and is not itself compliance-bearing."
                )
            if self.supervisee_purpose not in SuperviseeAssignmentPurpose.values:
                raise ValidationError(
                    "Assigning a BCBA as Supervisee requires a DEC-058 purpose: structured "
                    "professional development, consultation, competency development, "
                    "leadership development, or case discussion."
                )
        elif self.supervisee_purpose:
            raise ValidationError(
                "supervisee_purpose applies only when the Supervisee is a BCBA (DEC-058)."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SuperviseeIntake(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Exactly one intake record per supervisory relationship."""

    relationship = models.OneToOneField(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="intake",
    )
    # Background: structured selections, with `background` kept for free-text detail
    prior_aba_experience = models.CharField(
        max_length=32, choices=PriorAbaExperience.choices, blank=True, default=""
    )
    prior_aba_experience_other = models.CharField(max_length=255, blank=True, default="")
    current_context = models.JSONField(default=list, blank=True)
    current_context_other = models.CharField(max_length=255, blank=True, default="")
    background = models.TextField(blank=True, default="")
    current_credential = models.CharField(max_length=32, blank=True, default="")
    # Starting point: development domains, a separate assessment flag, and `starting_notes` for detail
    starting_domains = models.JSONField(default=list, blank=True)
    starting_domains_other = models.CharField(max_length=255, blank=True, default="")
    # None = never answered (including intakes captured before this question existed)
    further_assessment_needed = models.BooleanField(null=True, blank=True, default=None)
    starting_notes = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    captured_on = models.DateField()

    class Meta:
        db_table = "supervisee_intake"

    def clean(self):
        if self.relationship_id and self.organization_id:
            if self.relationship.organization_id != self.organization_id:
                raise ValidationError("Intake must belong to the same organization as the relationship.")
        if not isinstance(self.current_context, list) or not isinstance(self.starting_domains, list):
            raise ValidationError("Current context and starting domains must be lists.")
        _require_choices(self.current_context, CurrentContext.choices, "current context")
        _require_choices(self.starting_domains, DEVELOPMENT_DOMAIN_CHOICES, "starting point domain")
        # "Other" must say what it is; the text is dropped when "Other" isn't chosen
        for chosen, field, label in [
            (self.prior_aba_experience == PriorAbaExperience.OTHER, "prior_aba_experience_other", "Other ABA experience"),
            ("other" in self.current_context, "current_context_other", "Other current context"),
            ("other" in self.starting_domains, "starting_domains_other", "Other starting point domain"),
        ]:
            value = (getattr(self, field) or "").strip()
            if chosen and not value:
                raise ValidationError({field: f"Describe the {label}."})
            setattr(self, field, value if chosen else "")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class DevelopmentPlan(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Versioned IDP. Prior versions are preserved (never overwritten)."""

    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="development_plans",
    )
    plan_family_id = models.UUIDField()
    version_number = models.PositiveIntegerField()
    is_current = models.BooleanField(default=True)
    summary = models.TextField(blank=True, default="")

    class Meta:
        db_table = "development_plan"
        constraints = [
            models.UniqueConstraint(
                fields=["plan_family_id", "version_number"],
                name="unique_idp_version",
            ),
            models.UniqueConstraint(
                fields=["plan_family_id"],
                condition=models.Q(is_current=True),
                name="unique_current_idp_per_family",
            ),
        ]

    def clean(self):
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("summary", "record_status", "is_current", "version_number", "plan_family_id")
                .first()
            )
            if previous and previous["record_status"] == RecordStatus.SUPERSEDED:
                if self.summary != previous["summary"]:
                    raise ValidationError("Superseded development plan versions are immutable.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class DevelopmentPlanGoal(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    plan = models.ForeignKey(
        DevelopmentPlan,
        on_delete=models.PROTECT,
        related_name="goals",
    )
    # Blank only on goals written before categories existed
    category = models.CharField(max_length=64, choices=GOAL_CATEGORY_CHOICES, blank=True, default="")
    title = models.CharField(max_length=255)  # the supervisor's individualized goal statement
    description = models.TextField(blank=True, default="")
    status = models.CharField(max_length=32, default=RecordStatus.ACTIVE)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "development_plan_goal"
        ordering = ["sort_order", "created_at"]


class DevelopmentPlanCompetency(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    plan = models.ForeignKey(
        DevelopmentPlan,
        on_delete=models.PROTECT,
        related_name="competencies",
    )
    name = models.CharField(max_length=255)
    # Tags saved with the item (not looked up later) so library changes never alter earlier versions.
    # competency_key is the plan_library ID; blank for a custom competency.
    competency_key = models.CharField(max_length=64, blank=True, default="")
    domain = models.CharField(max_length=64, choices=DEVELOPMENT_DOMAIN_CHOICES, blank=True, default="")
    pathways = models.JSONField(default=list, blank=True)
    description = models.TextField(blank=True, default="")
    # Superseded versions may hold free-text levels from before CompetencyLevel; new input is validated in services
    target_level = models.CharField(max_length=64, choices=CompetencyLevel.choices, blank=True, default="")
    current_level = models.CharField(max_length=64, choices=CompetencyLevel.choices, blank=True, default="")
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "development_plan_competency"
        ordering = ["sort_order", "created_at"]


class DevelopmentPlanMilestone(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    class MilestoneStatus(models.TextChoices):
        ACTIVE = "active", "Active"
        ACHIEVED = "achieved", "Achieved"
        DISCONTINUED = "discontinued", "Discontinued"

    TERMINAL_STATUSES = frozenset({MilestoneStatus.ACHIEVED, MilestoneStatus.DISCONTINUED})

    plan = models.ForeignKey(
        DevelopmentPlan,
        on_delete=models.PROTECT,
        related_name="milestones",
    )
    title = models.CharField(max_length=255)  # the milestone as written, including any edits to the template
    template_key = models.CharField(max_length=64, blank=True, default="")  # plan_library template; blank = custom
    criterion_count = models.PositiveSmallIntegerField(null=True, blank=True)  # the template's [#]
    rationale = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=MilestoneStatus.choices,
        default=MilestoneStatus.ACTIVE,
    )
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "development_plan_milestone"
        ordering = ["sort_order", "created_at"]

    def clean(self):
        previous = None
        if self.pk:
            previous = (
                type(self)
                .objects.filter(pk=self.pk)
                .values("status", "rationale", "title")
                .first()
            )
        previous_status = previous["status"] if previous else None
        rationale = (self.rationale or "").strip()

        if previous_status in self.TERMINAL_STATUSES and self.status != previous_status:
            raise ValidationError(
                "Achieved or Discontinued milestones cannot change status. "
                "Revise the development plan to record a new version instead."
            )

        transitioning_to_terminal = self.status in self.TERMINAL_STATUSES and previous_status in (
            None,
            self.MilestoneStatus.ACTIVE,
        )
        if transitioning_to_terminal and previous_status == self.MilestoneStatus.ACTIVE and not rationale:
            raise ValidationError(
                "Milestone transition from Active to Achieved or Discontinued "
                "requires a documented rationale."
            )

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


# Tracks whose supervision hours are self-logged by the trainee and verified by the Supervisor
SELF_LOGGED_HOURS_TRACKS = frozenset({SupervisionTrack.SUPERVISED_FIELDWORK})
# Tracks whose supervision hours come only from Supervisor-authored session records, measured
# against the Supervisee's self-attested monthly service hours
SESSION_DERIVED_HOURS_TRACKS = frozenset(
    {SupervisionTrack.RBT_ONGOING, SupervisionTrack.BCABA_ONGOING}
)


def _same_month(day, cycle, label):
    if day and (day.year, day.month) != (cycle.year, cycle.month):
        raise ValidationError(f"{label} must fall within the cycle month ({cycle.year}-{cycle.month:02d}).")


class MonthlyCycle(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """One relationship's month: its assignments, logged hours and supervision sessions."""

    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="monthly_cycles",
    )
    year = models.PositiveIntegerField()
    month = models.PositiveSmallIntegerField()
    status = models.CharField(max_length=32, default=RecordStatus.ACTIVE)
    # Supervised Fieldwork only: each month is either supervised or concentrated, so a
    # relationship can mix the two across months
    fieldwork_type = models.CharField(
        max_length=32,
        choices=FieldworkSubtype.choices,
        blank=True,
        default="",
    )

    class Meta:
        db_table = "monthly_cycle"
        ordering = ["year", "month"]
        constraints = [
            models.UniqueConstraint(
                fields=["relationship", "year", "month"],
                name="unique_cycle_per_relationship_month",
            )
        ]

    def clean(self):
        if not 1 <= self.month <= 12:
            raise ValidationError("Month must be between 1 and 12.")
        is_fieldwork = self.relationship.supervision_track == SupervisionTrack.SUPERVISED_FIELDWORK
        if self.fieldwork_type and not is_fieldwork:
            raise ValidationError("Fieldwork type applies only to Supervised Fieldwork relationships.")
        if is_fieldwork and not self.fieldwork_type:
            raise ValidationError("A Supervised Fieldwork month must be supervised or concentrated.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class AssignmentStatus(models.TextChoices):
    ISSUED = "issued", "Issued"
    SUBMITTED = "submitted", "Submitted"
    REVISION_REQUESTED = "revision_requested", "Revision Requested"
    COMPLETED = "completed", "Completed"


class Assignment(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    cycle = models.ForeignKey(MonthlyCycle, on_delete=models.PROTECT, related_name="assignments")
    title = models.CharField(max_length=255)
    instructions = models.TextField(blank=True, default="")
    status = models.CharField(max_length=32, choices=AssignmentStatus.choices, default=AssignmentStatus.ISSUED)
    evidence_notes = models.TextField(blank=True, default="")
    due_on = models.DateField(null=True, blank=True)
    issued_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="issued_assignments",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "assignment"
        ordering = ["created_at"]


class AssignmentEvent(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Every lifecycle state an assignment passes through, in order. Never edited."""

    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="events")
    from_status = models.CharField(max_length=32, choices=AssignmentStatus.choices, blank=True, default="")
    to_status = models.CharField(max_length=32, choices=AssignmentStatus.choices)
    actor = models.ForeignKey("accounts.UserAccount", on_delete=models.PROTECT, related_name="assignment_events")
    comment = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assignment_event"
        ordering = ["created_at"]


def _attachment_path(instance, filename):
    return f"assignments/{instance.organization_id}/{instance.assignment_id}/{instance.id}_{filename}"


class AssignmentAttachment(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """A file submitted with an assignment. Tied to the submission event it arrived with."""

    assignment = models.ForeignKey(Assignment, on_delete=models.PROTECT, related_name="attachments")
    event = models.ForeignKey(AssignmentEvent, on_delete=models.PROTECT, related_name="attachments")
    file = models.FileField(upload_to=_attachment_path, max_length=500)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.PositiveIntegerField()
    uploaded_by = models.ForeignKey("accounts.UserAccount", on_delete=models.PROTECT, related_name="assignment_attachments")

    class Meta:
        db_table = "assignment_attachment"
        ordering = ["created_at"]


class HoursKind(models.TextChoices):
    INDEPENDENT = "independent", "Independent Fieldwork"
    SUPERVISION = "supervision", "Supervision"


class SupervisionFormat(models.TextChoices):
    INDIVIDUAL = "individual", "Individual"
    GROUP = "group", "Group"


class HoursStatus(models.TextChoices):
    PENDING = "pending", "Pending Review"
    VERIFIED = "verified", "Verified"
    RETURNED = "returned", "Returned for Correction"


class HoursEntry(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Trainee fieldwork hours — self-logged, then supervisor-verified. Only verified hours count.

    RBT ongoing-supervision hours are derived from session records, not this table.
    """

    cycle = models.ForeignKey(MonthlyCycle, on_delete=models.PROTECT, related_name="hours_entries")
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    attested_on = models.DateField()
    occurred_on = models.DateField()
    kind = models.CharField(max_length=32, choices=HoursKind.choices, default=HoursKind.INDEPENDENT)
    supervision_format = models.CharField(max_length=32, choices=SupervisionFormat.choices, blank=True, default="")
    client_observation = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")
    logged_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="logged_hours",
        null=True,
        blank=True,
    )
    status = models.CharField(max_length=32, choices=HoursStatus.choices, default=HoursStatus.PENDING)
    return_reason = models.TextField(blank=True, default="")
    verified = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True, blank=True)
    verified_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="verified_hours",
        null=True,
        blank=True,
    )

    class Meta:
        db_table = "hours_entry"
        ordering = ["occurred_on", "created_at"]

    def clean(self):
        relationship = self.cycle.relationship
        if relationship.supervision_track not in SELF_LOGGED_HOURS_TRACKS:
            raise ValidationError(
                "Self-logged hours apply only to Supervised Fieldwork. RBT ongoing-supervision hours "
                "are derived from Supervisor-authored session records."
            )
        if self.logged_by_id and self.logged_by_id != relationship.supervisee_id:
            raise ValidationError("Fieldwork hours are logged by the Supervisee.")
        if self.hours is None or not 0 < self.hours <= 24:
            raise ValidationError("Hours must be more than 0 and no more than 24 for one day.")
        _same_month(self.occurred_on, self.cycle, "The date worked")
        if self.kind == HoursKind.SUPERVISION:
            if not self.supervision_format:
                raise ValidationError("Supervision hours must be individual or group.")
        elif self.supervision_format or self.client_observation:
            raise ValidationError("Format and client observation apply only to supervision hours.")
        self.verified = self.status == HoursStatus.VERIFIED

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class SessionType(models.TextChoices):
    INDIVIDUAL = "individual", "Individual Supervision"
    GROUP = "group", "Group Supervision"


class SupervisionSession(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Compliance-bearing session documentation, authored by the Supervisor. Scheduling never grants credit."""

    cycle = models.ForeignKey(
        MonthlyCycle, on_delete=models.PROTECT, related_name="sessions"
    )
    occurred_on = models.DateField()
    duration_minutes = models.PositiveIntegerField()
    notes = models.TextField(blank=True, default="")
    session_type = models.CharField(max_length=32, choices=SessionType.choices, default=SessionType.INDIVIDUAL)
    client_observation = models.BooleanField(default=False)
    documented_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="documented_sessions",
    )

    class Meta:
        db_table = "supervision_session"
        ordering = ["occurred_on", "created_at"]

    def clean(self):
        if self.documented_by_id != self.cycle.relationship.supervisor_id:
            raise ValidationError("Supervision sessions are documented by the relationship's Supervisor.")
        if not self.duration_minutes or self.duration_minutes > 24 * 60:
            raise ValidationError("Duration must be between 1 minute and 24 hours.")
        _same_month(self.occurred_on, self.cycle, "The session date")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class ServiceHoursAttestation(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """The Supervisee's self-attested service hours for one month (RBT / BCaBA ongoing supervision).
    The denominator of the supervision percentage; never mixed with fieldwork hours."""

    cycle = models.ForeignKey(MonthlyCycle, on_delete=models.PROTECT, related_name="service_attestations")
    hours = models.DecimalField(max_digits=7, decimal_places=2)
    attested_by = models.ForeignKey("accounts.UserAccount", on_delete=models.PROTECT, related_name="service_attestations")
    attested_on = models.DateField()
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "service_hours_attestation"
        constraints = [
            models.UniqueConstraint(fields=["cycle"], name="unique_service_attestation_per_cycle"),
        ]

    def clean(self):
        relationship = self.cycle.relationship
        if relationship.supervision_track not in SESSION_DERIVED_HOURS_TRACKS:
            raise ValidationError(
                "Monthly service hours are attested only on RBT or BCaBA ongoing supervision. "
                "Supervised Fieldwork hours are logged and verified instead."
            )
        if self.attested_by_id != relationship.supervisee_id:
            raise ValidationError("Monthly service hours are attested by the Supervisee.")
        if self.hours is None or not 0 <= self.hours <= 744:
            raise ValidationError("Service hours must be between 0 and 744 for one month.")

    def save(self, *args, **kwargs):
        self.full_clean()
        return super().save(*args, **kwargs)


class Appointment(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    """Non-compliance-bearing logistics. Structurally separate from session records."""

    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="appointments",
    )
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField()
    status = models.CharField(max_length=32, default="scheduled")

    class Meta:
        db_table = "appointment"


class PreSupervisionNote(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    appointment = models.ForeignKey(
        Appointment, on_delete=models.PROTECT, related_name="prep_notes"
    )
    submitted_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="prep_notes",
    )
    body = models.TextField()

    class Meta:
        db_table = "pre_supervision_note"


class CompetencyAssessment(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="assessments",
    )
    assessed_on = models.DateField()
    rating_scale = models.CharField(max_length=64)
    rating = models.CharField(max_length=64)
    documented_content = models.TextField()
    approved = models.BooleanField(default=False)

    class Meta:
        db_table = "competency_assessment"


class ComplianceRuleSet(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel):
    """Versioned by effective date. Regulatory changes never require a schema change."""

    name = models.CharField(max_length=255)
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)
    supervision_track = models.CharField(max_length=64, choices=SupervisionTrack.choices)

    class Meta:
        db_table = "compliance_rule_set"


class ComplianceRuleParameter(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel):
    rule_set = models.ForeignKey(
        ComplianceRuleSet, on_delete=models.PROTECT, related_name="parameters"
    )
    key = models.CharField(max_length=128)
    value = models.CharField(max_length=128)
    unit = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        db_table = "compliance_rule_parameter"


class ComplianceReminder(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    cycle = models.ForeignKey(
        MonthlyCycle, on_delete=models.PROTECT, related_name="reminders"
    )
    message = models.TextField()
    status = models.CharField(max_length=32, default="open")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "compliance_reminder"


class ReviewComment(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="review_comments",
    )
    author = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        related_name="review_comments",
    )
    body = models.TextField()
    target_entity_type = models.CharField(max_length=64)
    target_entity_id = models.UUIDField()

    class Meta:
        db_table = "review_comment"


class RelationshipConclusion(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    relationship = models.OneToOneField(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="conclusion",
    )
    reason = models.CharField(max_length=255)
    concluded_on = models.DateField()
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "relationship_conclusion"


class AISuggestion(UUIDPrimaryKeyModel, TimestampedModel, SoftArchiveModel, OrganizationScopedModel):
    relationship = models.ForeignKey(
        SupervisoryRelationship,
        on_delete=models.PROTECT,
        related_name="ai_suggestions",
    )
    suggestion_identifier = models.UUIDField(unique=True)
    prompt = models.TextField()
    model_name = models.CharField(max_length=128)
    explanation = models.TextField()
    disposition = models.CharField(max_length=32, default="pending")
    dispositioned_by = models.ForeignKey(
        "accounts.UserAccount",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="ai_dispositions",
    )

    class Meta:
        db_table = "ai_suggestion"
