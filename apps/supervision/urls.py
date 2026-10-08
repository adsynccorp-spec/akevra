from django.urls import path

from apps.supervision import monthly_views as mv

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
    # Monthly Work & Hours
    path("relationships/<uuid:pk>/cycles", mv.CycleListView.as_view(), name="relationship-cycles"),
    path("relationships/<uuid:pk>/compliance", mv.ComplianceView.as_view(), name="relationship-compliance"),
    path("relationships/<uuid:pk>/assignments", mv.AssignmentListView.as_view(), name="relationship-assignments"),
    path("relationships/<uuid:pk>/hours", mv.HoursListView.as_view(), name="relationship-hours"),
    path("relationships/<uuid:pk>/sessions", mv.SessionListView.as_view(), name="relationship-sessions"),
    path("relationships/<uuid:pk>/service-hours", mv.ServiceHoursView.as_view(), name="relationship-service-hours"),
    path("cycles/<uuid:pk>", mv.CycleDetailView.as_view(), name="cycle-detail"),
    path("assignments/<uuid:pk>", mv.AssignmentDetailView.as_view(), name="assignment-detail"),
    path("assignments/<uuid:pk>/submit", mv.AssignmentSubmitView.as_view(), name="assignment-submit"),
    path("assignments/<uuid:pk>/request-revision", mv.AssignmentRevisionView.as_view(), name="assignment-request-revision"),
    path("assignments/<uuid:pk>/complete", mv.AssignmentCompleteView.as_view(), name="assignment-complete"),
    path("assignment-attachments/<uuid:pk>/download", mv.AttachmentDownloadView.as_view(), name="assignment-attachment-download"),
    path("hours/<uuid:pk>", mv.HoursDetailView.as_view(), name="hours-detail"),
    path("hours/<uuid:pk>/verify", mv.HoursVerifyView.as_view(), name="hours-verify"),
    path("hours/<uuid:pk>/return", mv.HoursReturnView.as_view(), name="hours-return"),
]
