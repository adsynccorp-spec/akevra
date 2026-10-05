from django.contrib import admin

from apps.rbac.models import OrganizationRoleGrant


@admin.register(OrganizationRoleGrant)
class OrganizationRoleGrantAdmin(admin.ModelAdmin):
    list_display = ("user_account", "role", "organization")
