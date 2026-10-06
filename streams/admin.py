from django.contrib import admin

from .models import Drone, Mission, Stream, StreamLog


@admin.register(Drone)
class DroneAdmin(admin.ModelAdmin):
    list_display = ("drone_name", "serial_number", "model")
    search_fields = ("drone_name", "serial_number", "model")


@admin.register(Mission)
class MissionAdmin(admin.ModelAdmin):
    list_display = ("mission_name", "location")
    search_fields = ("mission_name", "location")


@admin.register(Stream)
class StreamAdmin(admin.ModelAdmin):
    list_display = ("stream_name", "drone", "mission", "stream_key", "is_active", "is_live", "created_at")
    list_filter = ("is_active", "is_live", "created_at")
    search_fields = ("stream_name", "stream_key", "drone__drone_name", "mission__mission_name")
    readonly_fields = ("created_at", "updated_at", "live_started_at", "last_activity_at")
    actions = ("enable_streams", "disable_streams", "regenerate_stream_keys")

    @admin.action(description="Enable selected streams")
    def enable_streams(self, request, queryset):
        queryset.update(is_active=True)
        for stream in queryset:
            StreamLog.objects.create(stream=stream, event=StreamLog.Event.ENABLED)

    @admin.action(description="Disable selected streams")
    def disable_streams(self, request, queryset):
        queryset.update(is_active=False)
        for stream in queryset:
            StreamLog.objects.create(stream=stream, event=StreamLog.Event.DISABLED)

    @admin.action(description="Regenerate selected stream keys")
    def regenerate_stream_keys(self, request, queryset):
        for stream in queryset:
            stream.regenerate_stream_key()
            StreamLog.objects.create(stream=stream, event=StreamLog.Event.KEY_REGENERATED)


@admin.register(StreamLog)
class StreamLogAdmin(admin.ModelAdmin):
    list_display = ("stream", "event", "timestamp")
    list_filter = ("event", "timestamp")
    search_fields = ("stream__stream_name", "event")
