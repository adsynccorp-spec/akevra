from django.urls import path

from apps.supervision import ai_views as aiv

urlpatterns = [
    path("relationships/<uuid:pk>/ai-suggestions", aiv.RelationshipSuggestionsView.as_view(), name="relationship-ai-suggestions"),
    path("relationships/<uuid:pk>/ai-suggestions/generate", aiv.GenerateSuggestionsView.as_view(), name="relationship-ai-suggestions-generate"),
    path("ai-suggestions", aiv.AllSuggestionsView.as_view(), name="ai-suggestions"),
    path("ai-suggestions/<uuid:pk>", aiv.SuggestionDetailView.as_view(), name="ai-suggestion-detail"),
    path("ai-suggestions/<uuid:pk>/disposition", aiv.SuggestionDispositionView.as_view(), name="ai-suggestion-disposition"),
    path("ai-suggestions/<uuid:pk>/reveal", aiv.SuggestionRevealView.as_view(), name="ai-suggestion-reveal"),
]
