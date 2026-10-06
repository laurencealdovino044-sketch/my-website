from django.contrib import admin

# Register your models here.
from .models import (
    ActivityHistory,
    GoogleIdentity,
    Notification,
    NotificationPreference,
    ScheduledApplication,
)


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ('user', 'phone_number', 'email_enabled', 'sms_enabled', 'notify_on_detection', 'updated_at')
    list_filter = ('email_enabled', 'sms_enabled', 'notify_on_detection', 'notify_on_schedule')
    search_fields = ('user__username', 'user__email', 'phone_number')


@admin.register(ActivityHistory)
class ActivityHistoryAdmin(admin.ModelAdmin):
    list_display = ('user', 'action', 'timestamp')
    list_filter = ('action',)
    search_fields = ('user__username', 'details')


@admin.register(ScheduledApplication)
class ScheduledApplicationAdmin(admin.ModelAdmin):
    list_display = ('user', 'application_type', 'due_date', 'completed')
    list_filter = ('application_type', 'completed')
    search_fields = ('user__username',)


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ('user', 'title', 'category', 'level', 'is_read', 'created_at')
    list_filter = ('category', 'level', 'is_read')
    search_fields = ('user__username', 'title', 'message')


@admin.register(GoogleIdentity)
class GoogleIdentityAdmin(admin.ModelAdmin):
    list_display = ('user', 'email', 'created_at', 'last_login_at')
    search_fields = ('user__username', 'email', 'sub')
    readonly_fields = ('sub', 'created_at', 'last_login_at')

# Accounts are provisioned locally; the web admin must not create extra users.
from django.contrib.auth import get_user_model

if admin.site.is_registered(get_user_model()):
    admin.site.unregister(get_user_model())
