from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.core.views import CurrentOrganizationView, HealthView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
    path("api/v1/health", HealthView.as_view(), name="health"),
    path("api/v1/organization", CurrentOrganizationView.as_view(), name="current-org"),
    path("api/v1/auth/", include("apps.accounts.urls")),
    path("api/v1/rbac/", include("apps.rbac.urls")),
    path("api/v1/users/", include("apps.accounts.user_urls")),
    path("api/v1/", include("apps.supervision.urls")),
    path("api/v1/audit/", include("apps.audit.urls")),
]
