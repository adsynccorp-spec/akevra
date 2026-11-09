from django.urls import path

from apps.supervision import review_views as rv

urlpatterns = [
    # A5 review comments (immutable: no edit or delete routes)
    path("review-comments", rv.ReviewCommentsView.as_view(), name="review-comments"),
    path("relationships/<uuid:pk>/review-comments", rv.RelationshipReviewCommentsView.as_view(), name="relationship-review-comments"),
    # A4 audit trail
    path("audit/trail", rv.AuditTrailView.as_view(), name="audit-trail"),
    path("relationships/<uuid:pk>/audit-trail", rv.RelationshipAuditTrailView.as_view(), name="relationship-audit-trail"),
]
