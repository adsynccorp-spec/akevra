from django.urls import path

from apps.accounts.invitation_views import InvitationActionView, InvitationListView

urlpatterns = [
    path("invitations", InvitationListView.as_view(), name="invitation-list"),
    path("invitations/<uuid:pk>/resend", InvitationActionView.as_view(action="resend"), name="invitation-resend"),
    path("invitations/<uuid:pk>/revoke", InvitationActionView.as_view(action="revoke"), name="invitation-revoke"),
]
