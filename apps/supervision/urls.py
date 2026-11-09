from django.urls import path

from apps.supervision import monthly_views as mv
from apps.supervision import scheduling_views as sv
from apps.supervision import assessment_views as av
from apps.supervision import notification_views as nv
from apps.supervision import period_views as pv
from apps.supervision import settings_views as stv
from apps.supervision import overview_views as ov

from apps.supervision.views import (
    CompetencyListView,
    DashboardSummaryView,
    DevelopmentPlanDetailView,
    DevelopmentPlanListView,
    EligiblePartiesView,
    FieldworkStandardHistoryView,
    FieldworkStandardTraineesView,
    IntakeView,
    MilestoneTransitionView,
    PriorHoursReviewView,
    PlanLibraryView,
    RelationshipDetailView,
    RelationshipListView,
)

from apps.supervision import closure_views as clv  # noqa: E402
from apps.supervision import reminder_views as rmv  # noqa: E402

urlpatterns = [
    path("dashboard/summary", DashboardSummaryView.as_view(), name="dashboard-summary"),
    path("competencies", CompetencyListView.as_view(), name="competencies"),
    path("eligible-parties", EligiblePartiesView.as_view(), name="eligible-parties"),
    path("relationships", RelationshipListView.as_view(), name="relationship-list"),
    path("relationships/<uuid:pk>", RelationshipDetailView.as_view(), name="relationship-detail"),
    path("relationships/<uuid:pk>/intake", IntakeView.as_view(), name="relationship-intake"),
    path("relationships/<uuid:pk>/prior-hours/review", PriorHoursReviewView.as_view(), name="relationship-prior-hours-review"),
    path("fieldwork-standard/trainees", FieldworkStandardTraineesView.as_view(), name="fieldwork-standard-trainees"),
    path("relationships/<uuid:pk>/fieldwork-standard/history", FieldworkStandardHistoryView.as_view(), name="relationship-fieldwork-standard-history"),
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
    path("assignments", mv.AllAssignmentsView.as_view(), name="assignments-all"),
    path("hours", mv.AllHoursView.as_view(), name="hours-all"),
    path("sessions", mv.AllSessionsView.as_view(), name="sessions-all"),
    path("cycles/<uuid:pk>", mv.CycleDetailView.as_view(), name="cycle-detail"),
    path("assignments/<uuid:pk>", mv.AssignmentDetailView.as_view(), name="assignment-detail"),
    path("assignments/<uuid:pk>/submit", mv.AssignmentSubmitView.as_view(), name="assignment-submit"),
    path("assignments/<uuid:pk>/request-revision", mv.AssignmentRevisionView.as_view(), name="assignment-request-revision"),
    path("assignments/<uuid:pk>/complete", mv.AssignmentCompleteView.as_view(), name="assignment-complete"),
    path("assignment-attachments/<uuid:pk>/download", mv.AttachmentDownloadView.as_view(), name="assignment-attachment-download"),
    path("assignments/<uuid:pk>/issue", mv.AssignmentIssueView.as_view(), name="assignment-issue"),
    path("sessions/<uuid:pk>", mv.SessionDetailView.as_view(), name="session-detail"),
    path("session-attachments/<uuid:pk>/download", mv.SessionAttachmentDownloadView.as_view(), name="session-attachment-download"),
    path("relationships/<uuid:pk>/notes", mv.RelationshipNotesView.as_view(), name="relationship-notes"),
    # Scheduling (logistics only; never compliance-bearing)
    path("appointments", sv.AllAppointmentsView.as_view(), name="appointments-all"),
    path("relationships/<uuid:pk>/appointments", sv.AppointmentListView.as_view(), name="relationship-appointments"),
    path("appointments/<uuid:pk>", sv.AppointmentDetailView.as_view(), name="appointment-detail"),
    path("availability", sv.MyAvailabilityView.as_view(), name="availability-mine"),
    path("relationships/<uuid:pk>/availability", sv.RelationshipAvailabilityView.as_view(), name="relationship-availability"),
    path("appointments/<uuid:pk>/confirm", sv.AppointmentConfirmView.as_view(), name="appointment-confirm"),
    path("appointments/<uuid:pk>/cancel", sv.AppointmentCancelView.as_view(), name="appointment-cancel"),
    path("appointments/<uuid:pk>/complete", sv.AppointmentCompleteView.as_view(), name="appointment-complete"),
    path("appointments/<uuid:pk>/no-show", sv.AppointmentNoShowView.as_view(), name="appointment-no-show"),
    path("appointments/<uuid:pk>/prep-note", sv.PrepNoteView.as_view(), name="appointment-prep-note"),
    path("relationships/<uuid:pk>/preparation", sv.PreparationTemplateView.as_view(), name="relationship-preparation"),
    # Competency assessments
    path("relationships/<uuid:pk>/assessments", av.AssessmentListView.as_view(), name="relationship-assessments"),
    path("assessments/<uuid:pk>", av.AssessmentDetailView.as_view(), name="assessment-detail"),
    path("assessments/<uuid:pk>/submit", av.AssessmentSubmitView.as_view(), name="assessment-submit"),
    path("assessments/<uuid:pk>/return", av.AssessmentReturnView.as_view(), name="assessment-return"),
    path("assessments/<uuid:pk>/finalize", av.AssessmentFinalizeView.as_view(), name="assessment-finalize"),
    # Reporting periods, shared service hours, audit readiness
    path("cycles/<uuid:pk>/status", pv.CycleStatusView.as_view(), name="cycle-status"),
    path("supervisees/<uuid:pk>/service-hours", pv.SuperviseeServiceHoursView.as_view(), name="supervisee-service-hours"),
    path("relationships/<uuid:pk>/service-hours/periods", pv.ServiceHoursPeriodView.as_view(), name="relationship-service-hours-periods"),
    path("supervisees/<uuid:pk>/external-supervision", pv.ExternalSupervisionView.as_view(), name="supervisee-external-supervision"),
    path("external-supervision/<uuid:pk>/void", pv.ExternalSupervisionVoidView.as_view(), name="external-supervision-void"),
    path("service-hours/<uuid:pk>/review", pv.ServiceHoursReviewView.as_view(), name="service-hours-review"),
    path("supervisees/<uuid:pk>/coordinators", pv.CoordinatorListView.as_view(), name="supervisee-coordinators"),
    path("coordinators/<uuid:pk>", pv.CoordinatorDetailView.as_view(), name="coordinator-detail"),
    path("people/<uuid:pk>/certification", pv.CertificationView.as_view(), name="person-certification"),
    path("supervisees/<uuid:pk>/audit-log", pv.AuditLogView.as_view(), name="supervisee-audit-log"),
    path("audit/people", pv.AuditPeopleView.as_view(), name="audit-people"),
    # Settings: compliance rule approval and organization settings
    path("compliance-rules", stv.ComplianceRulesView.as_view(), name="compliance-rules"),
    path("compliance-rules/approve", stv.ComplianceRulesApproveView.as_view(), name="compliance-rules-approve"),
    path("organization/settings", stv.OrganizationSettingsView.as_view(), name="organization-settings"),
    # Notifications (the bell)
    path("notifications", nv.NotificationListView.as_view(), name="notifications"),
    path("notifications/read-all", nv.NotificationReadAllView.as_view(), name="notifications-read-all"),
    path("notifications/<uuid:pk>/read", nv.NotificationReadView.as_view(), name="notification-read"),
    path("hours/<uuid:pk>", mv.HoursDetailView.as_view(), name="hours-detail"),
    path("hours/<uuid:pk>/verify", mv.HoursVerifyView.as_view(), name="hours-verify"),
    path("hours/<uuid:pk>/return", mv.HoursReturnView.as_view(), name="hours-return"),
    path("hours/<uuid:pk>/observation-minutes", mv.HoursObservationMinutesView.as_view(), name="hours-observation-minutes"),
    # Read-only overviews across my relationships (Compliance, Competencies, Reports & Audit, Settings)
    path("compliance/overview", ov.ComplianceOverviewView.as_view(), name="compliance-overview"),
    path("competencies/overview", ov.CompetencyOverviewView.as_view(), name="competency-overview"),
    path("audit/activity", ov.AuditActivityView.as_view(), name="audit-activity"),
    path("account/sessions", ov.MySessionsView.as_view(), name="account-sessions"),
    path("account/profile", ov.MyProfileView.as_view(), name="account-profile"),
    path("relationships/<uuid:pk>/reminders", rmv.RelationshipRemindersView.as_view(), name="relationship-reminders"),
    path("reminders", rmv.AllRemindersView.as_view(), name="reminders-all"),
    path("reminders/<uuid:pk>/resolve", rmv.ReminderActionView.as_view(action="resolve"), name="reminder-resolve"),
    path("reminders/<uuid:pk>/dismiss", rmv.ReminderActionView.as_view(action="dismiss"), name="reminder-dismiss"),
    path("relationships/<uuid:pk>/conclude", clv.ConcludeRelationshipView.as_view(), name="relationship-conclude"),
    path("conclusion-reasons", clv.ConclusionReasonsView.as_view(), name="conclusion-reasons"),
    path("relationships/<uuid:pk>/export", clv.RelationshipExportView.as_view(), name="relationship-export"),
]
