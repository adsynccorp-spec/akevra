from django.contrib import admin

from apps.audit.models import AuditEvent


@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    list_display = ("created_at", "action", "entity_type", "organization")
    readonly_fields = [field.name for field in AuditEvent._meta.fields]

    def has_delete_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False
