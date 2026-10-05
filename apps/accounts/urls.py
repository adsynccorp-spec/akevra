from django.urls import path

from apps.accounts.invitation_views import (
    InvitationActivateView,
    InvitationDetailView,
    InvitationVerifyView,
)
from apps.accounts.password_views import PasswordForgotView, PasswordResetView, PasswordVerifyView
from apps.accounts.views import (
    LoginView,
    LogoutView,
    MeView,
    MFAEnrollView,
    MFAVerifyView,
    SessionView,
    WorkspaceSelectView,
)

urlpatterns = [
    path("login", LoginView.as_view(), name="login"),
    path("mfa/verify", MFAVerifyView.as_view(), name="mfa-verify"),
    path("mfa/enroll", MFAEnrollView.as_view(), name="mfa-enroll"),
    path("workspace/select", WorkspaceSelectView.as_view(), name="workspace-select"),
    path("logout", LogoutView.as_view(), name="logout"),
    path("session", SessionView.as_view(), name="session"),
    path("me", MeView.as_view(), name="me"),
    path("password/forgot", PasswordForgotView.as_view(), name="password-forgot"),
    path("password/verify", PasswordVerifyView.as_view(), name="password-verify"),
    path("password/reset", PasswordResetView.as_view(), name="password-reset"),
    path("invitations/<str:token>", InvitationDetailView.as_view(), name="invitation-detail"),
    path("invitations/<str:token>/verify", InvitationVerifyView.as_view(), name="invitation-verify"),
    path("invitations/<str:token>/activate", InvitationActivateView.as_view(), name="invitation-activate"),
]
