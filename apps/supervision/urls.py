from django.urls import path

from apps.supervision.views import (
    CompetencyListView,
    DashboardSummaryView,
    DevelopmentPlanDetailView,
    DevelopmentPlanListView,
    EligiblePartiesView,
    IntakeView,
    MilestoneTransitionView,
    PlanLibraryView,
    RelationshipDetailView,
    RelationshipListView,
)

urlpatterns = [
    path("dashboard/summary", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("competencies", CompetencyListView.as_view(), name="competencies"),
    path("eligible-parties", EligiblePartiesView.as_view(), name="eligible-parties"),
    path("relationships", RelationshipListView.as_view(), name="relationship-list"),
    path("relationships/<uuid:pk>", RelationshipDetailView.as_view(), name="relationship-detail"),
    path("relationships/<uuid:pk>/intake", IntakeView.as_view(), name="relationship-intake"),
    path(
        "relationships/<uuid:pk>/development-plans",
        DevelopmentPlanListView.as_view(),
        name="relationship-development-plans",
    ),
    path("plan-library", PlanLibraryView.as_view(), name="plan-library"),
    path("development-plans/<uuid:pk>", DevelopmentPlanDetailView.as_view(), name="development-plan-detail"),
    path("milestones/<uuid:pk>/transition", MilestoneTransitionView.as_view(), name="milestone-transition"),
]
