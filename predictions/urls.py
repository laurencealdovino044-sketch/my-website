from django.urls import path
from django.conf.urls.static import static
from django.conf import settings
from . import views

urlpatterns = [
    path('', views.landing_page, name='landing_page'),
    path('home/', views.home, name='home'),
    path('signup/', views.signup, name='signup'),
    path('login/', views.login_view, name='login'),
    path('logout/', views.logout_view, name='logout'),

    # "Continue with Google": verifies the ID token from the Sign in with Google button
    path('auth/google/', views.google_login, name='google_login'),
    path('predict/', views.predict, name='predict'),
    path('start_live_scan/', views.start_live_scan, name='start_live_scan'),
    path('stop_live_scan/', views.stop_live_scan, name='stop_live_scan'),
    path('about/', views.about, name='about'),
    path('profile/', views.profile, name='profile'),
    path('edit_profile/', views.edit_profile, name='edit_profile'),

    # Where disease alerts and schedule reminders are sent (e-mail / SMS)
    path('notifications/', views.notification_settings, name='notification_settings'),
    path('notifications/test/', views.send_test_notification, name='send_test_notification'),

    # In-app notifications: the bell in the sidebar polls the feed; the inbox lists everything
    path('notifications/inbox/', views.notification_inbox, name='notification_inbox'),
    path('notifications/feed/', views.notification_feed, name='notification_feed'),
    path('notifications/read/', views.mark_notifications_read, name='mark_notifications_read'),
    path('result/', views.result, name='result'),
    path('variety-guide/', views.variety_guide, name='variety_guide'),
    path('log_season_incident/', views.log_season_incident, name='log_season_incident'),

    # Automated control pages referenced from base sidebar
    path('automated-watering/', views.automated_watering, name='automated_watering'),
    path('automated-spraying/', views.automated_spraying, name='automated_spraying'),
    path('fertilizer/', views.fertilizer_application, name='fertilizer_application'),
    path('history/', views.history, name='history'),

    # Action logging endpoint used by home template forms
    path('log_action/', views.log_action, name='log_action'),

    # Start a new season (home page form uses this name)
    path('start_new_season/', views.start_new_season, name='start_new_season'),

    # Sensor data API used by home page JS
    path('get_sensor_data/', views.get_sensor_data, name='get_sensor_data'),

    # Historical analytics for charts on the home dashboard
    path('get_analytics_data/', views.get_analytics_data, name='get_analytics_data'),

    # Weather proxy endpoint used by home page JS
    path('api/weather/', views.get_weather, name='api_weather'),

    # Endpoint to create RTMP publish credentials (rtmp base + stream key) for a user
    path('create_rtmp_stream/', views.create_rtmp_stream, name='create_rtmp_stream'),

    # Poll whether the user's drone stream is actually live (backed by MediaMTX, not a guess)
    path('live_stream_status/', views.live_stream_status, name='live_stream_status'),

    # Arm/revoke the drone's publish key. The drone pushes RTMP straight to MediaMTX, so these
    # gate the stream key rather than starting or stopping any local encoder.
    path('start_drone_scan/', views.start_drone_scan, name='start_drone_scan'),
    path('stop_drone_scan/', views.stop_drone_scan, name='stop_drone_scan'),

    # Run disease detection on a still grabbed from the live drone feed
    path('capture_live_frame/', views.capture_live_frame, name='capture_live_frame'),

] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
