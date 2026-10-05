from django.conf import settings
from drf_spectacular.utils import extend_schema
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import password_reset as reset
from apps.accounts.passwords import PasswordPolicyError
from apps.core.api_serializers import (
    JSON_RESPONSE,
    PasswordCodeSerializer,
    PasswordForgotSerializer,
    PasswordResetSerializer,
)


class PublicPasswordView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]


class PasswordForgotView(PublicPasswordView):
    @extend_schema(tags=["Auth"], summary="Forgot password: email a reset code", request=PasswordForgotSerializer, responses=JSON_RESPONSE)
    def post(self, request):
        reset.request_reset(request.data.get("email"))
        # Same answer for every email so this cannot be used to discover accounts
        return Response({
            "detail": "If an AKEVRA account exists for this email, a verification code has been sent.",
            "expires_in": settings.PASSWORD_RESET_TTL_MINUTES * 60,
            "resend_after": int(reset.RESEND_COOLDOWN.total_seconds()),
        })


class PasswordVerifyView(PublicPasswordView):
    @extend_schema(tags=["Auth"], summary="Forgot password: check the emailed code", request=PasswordCodeSerializer, responses=JSON_RESPONSE)
    def post(self, request):
        try:
            reset.verify_code(request.data.get("email"), request.data.get("code"))
        except reset.ResetError as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"valid": True})


class PasswordResetView(PublicPasswordView):
    @extend_schema(tags=["Auth"], summary="Forgot password: set a new password", request=PasswordResetSerializer, responses=JSON_RESPONSE)
    def post(self, request):
        try:
            reset.reset_password(request.data.get("email"), request.data.get("code"), request.data.get("password"))
        except (reset.ResetError, PasswordPolicyError) as exc:
            return Response({"detail": str(exc)}, status=400)
        return Response({"status": "password_reset"})
