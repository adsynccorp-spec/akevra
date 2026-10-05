from apps.core import rls


class TenantIsolationMiddleware:
    """Bind every request to the akevra_app DB role and a tenant GUC context."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.organization = None
        request.user_account = None
        request.auth_session = None

        rls.apply_app_role()
        rls.clear_tenant_context()
        try:
            response = self.get_response(request)
            return response
        finally:
            rls.clear_tenant_context()
            rls.reset_role()
