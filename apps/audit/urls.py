from django.urls import path

from apps.audit.views import AuditListView

urlpatterns = [
    path("events", AuditListView.as_view(), name="audit-events"),
]
