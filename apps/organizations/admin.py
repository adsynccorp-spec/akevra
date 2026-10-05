from django.contrib import admin

from apps.organizations.models import Organization


@admin.register(Organization)
class OrganizationAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "status")
    search_fields = ("name", "slug")
