from django.contrib import admin

from apps.accounts.models import AuthSession, Identity, Invitation, UserAccount


@admin.register(Identity)
class IdentityAdmin(admin.ModelAdmin):
    list_display = ("email", "is_active", "mfa_enabled", "locked_until")
    search_fields = ("email",)


@admin.register(UserAccount)
class UserAccountAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "credential_type", "record_status")
    search_fields = ("email", "first_name", "last_name")


@admin.register(AuthSession)
class AuthSessionAdmin(admin.ModelAdmin):
    list_display = ("identity", "organization", "workspace_selected", "revoked_at")


@admin.register(Invitation)
class InvitationAdmin(admin.ModelAdmin):
    list_display = ("email", "organization", "credential_type", "org_role", "status", "expires_at")
    list_filter = ("status",)
    search_fields = ("email",)
    exclude = ("token_hash", "mfa_secret_encrypted")
