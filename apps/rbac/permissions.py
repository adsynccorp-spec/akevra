from rest_framework.permissions import BasePermission

from apps.rbac.services import has_permission


class HasWorkspace(BasePermission):
    def has_permission(self, request, view):
        return bool(getattr(request, "user_account", None) and getattr(request, "organization", None))


class HasRBACPermission(BasePermission):
    permission_name = None

    def has_permission(self, request, view):
        if not getattr(request, "user_account", None):
            return False
        name = getattr(view, "rbac_permission", self.permission_name)
        if not name:
            return True
        relationship = getattr(view, "relationship", None)
        return has_permission(request.user_account, name, relationship=relationship)

    def has_object_permission(self, request, view, obj):
        name = getattr(view, "rbac_permission", self.permission_name)
        relationship = obj if obj.__class__.__name__ == "SupervisoryRelationship" else getattr(obj, "relationship", None)
        return has_permission(request.user_account, name, relationship=relationship)
