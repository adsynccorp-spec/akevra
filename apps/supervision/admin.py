from django.contrib import admin

from apps.supervision.models import (
    DevelopmentPlan,
    DevelopmentPlanCompetency,
    DevelopmentPlanGoal,
    DevelopmentPlanMilestone,
    SuperviseeIntake,
    SupervisoryRelationship,
)


@admin.register(SupervisoryRelationship)
class SupervisoryRelationshipAdmin(admin.ModelAdmin):
    list_display = (
        "supervisor",
        "supervisee",
        "supervision_track",
        "supervisee_purpose",
        "organization",
        "status",
    )


@admin.register(SuperviseeIntake)
class SuperviseeIntakeAdmin(admin.ModelAdmin):
    list_display = ("relationship", "captured_on", "current_credential", "organization")


@admin.register(DevelopmentPlan)
class DevelopmentPlanAdmin(admin.ModelAdmin):
    list_display = ("relationship", "version_number", "is_current", "record_status", "organization")


@admin.register(DevelopmentPlanGoal)
class DevelopmentPlanGoalAdmin(admin.ModelAdmin):
    list_display = ("title", "plan", "status", "organization")


@admin.register(DevelopmentPlanCompetency)
class DevelopmentPlanCompetencyAdmin(admin.ModelAdmin):
    list_display = ("name", "plan", "target_level", "organization")


@admin.register(DevelopmentPlanMilestone)
class DevelopmentPlanMilestoneAdmin(admin.ModelAdmin):
    list_display = ("title", "plan", "status", "organization")
