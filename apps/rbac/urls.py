from django.urls import path

from apps.rbac.views import EvaluateView, MatrixView

urlpatterns = [
    path("matrix", MatrixView.as_view(), name="rbac-matrix"),
    path("evaluate", EvaluateView.as_view(), name="rbac-evaluate"),
]
