from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api_serializers import JSON_RESPONSE
from apps.rbac.permissions import HasWorkspace
from apps.supervision.models import Notification
from apps.supervision.monthly_views import _not_found
from apps.supervision.notifications import maybe_send_preparation_reminders, serialize


def _mine(request):
    return Notification.objects.filter(recipient=request.user_account, record_status="active")


class NotificationListView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Notifications"], summary="My latest notifications and the unread count", operation_id="notifications_list", responses=JSON_RESPONSE)
    def get(self, request):
        maybe_send_preparation_reminders(request.user_account.organization_id)
        mine = _mine(request)
        return Response({
            "unread": mine.filter(read_at__isnull=True).count(),
            "notifications": [serialize(n) for n in mine.order_by("-created_at")[:50]],
        })


class NotificationReadView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Notifications"], summary="Mark one notification read", request=None, operation_id="notifications_read", responses=JSON_RESPONSE)
    def post(self, request, pk):
        notification = _mine(request).filter(pk=pk).first()
        if notification is None:
            return _not_found()
        if notification.read_at is None:
            notification.read_at = timezone.now()
            notification.save(update_fields=["read_at", "updated_at"])
        return Response(serialize(notification))


class NotificationReadAllView(APIView):
    permission_classes = [HasWorkspace]

    @extend_schema(tags=["Notifications"], summary="Mark all my notifications read", request=None, operation_id="notifications_read_all", responses=JSON_RESPONSE)
    def post(self, request):
        count = _mine(request).filter(read_at__isnull=True).update(read_at=timezone.now(), updated_at=timezone.now())
        return Response({"marked_read": count})
