from django.urls import include, path
from rest_framework.routers import DefaultRouter
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .api import LiveStreamListAPIView, MediaMTXAuthAPIView, StreamViewSet

router = DefaultRouter()
router.register("streams", StreamViewSet, basename="stream")

urlpatterns = [
    path("token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("mediamtx/auth/", MediaMTXAuthAPIView.as_view(), name="mediamtx-auth"),
    path("live/", LiveStreamListAPIView.as_view(), name="live-streams"),
    path("", include(router.urls)),
]
