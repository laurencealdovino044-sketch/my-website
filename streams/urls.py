from django.urls import path
from django.views.generic import RedirectView

from .views import (
    StreamCreateView,
    StreamDeleteView,
    StreamDetailView,
    StreamListView,
    StreamLoginView,
    StreamLogoutView,
    StreamRegenerateKeyView,
    StreamToggleActiveView,
    StreamUpdateView,
    StreamViewerView,
)

app_name = "streams"

urlpatterns = [
    path("auth/login/", StreamLoginView.as_view(), name="login"),
    path("auth/logout/", StreamLogoutView.as_view(), name="logout"),
    path("dashboard/", RedirectView.as_view(url="/predictions/", permanent=False), name="dashboard"),
    path("", StreamListView.as_view(), name="stream-list"),
    path("create/", StreamCreateView.as_view(), name="stream-create"),
    path("<int:pk>/", StreamDetailView.as_view(), name="stream-detail"),
    path("<int:pk>/edit/", StreamUpdateView.as_view(), name="stream-edit"),
    path("<int:pk>/delete/", StreamDeleteView.as_view(), name="stream-delete"),
    path("<int:pk>/viewer/", StreamViewerView.as_view(), name="stream-viewer"),
    path("<int:pk>/regenerate/", StreamRegenerateKeyView.as_view(), name="stream-regenerate"),
    path("<int:pk>/toggle/", StreamToggleActiveView.as_view(), name="stream-toggle"),
]
