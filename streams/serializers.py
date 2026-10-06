from rest_framework import serializers

from .models import Drone, Mission, Stream


class DroneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Drone
        fields = ("id", "drone_name", "serial_number", "model")


class MissionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mission
        fields = ("id", "mission_name", "location", "description")


class StreamSerializer(serializers.ModelSerializer):
    drone = DroneSerializer(read_only=True)
    mission = MissionSerializer(read_only=True)
    drone_id = serializers.PrimaryKeyRelatedField(source="drone", queryset=Drone.objects.all(), required=False, allow_null=True, write_only=True)
    mission_id = serializers.PrimaryKeyRelatedField(source="mission", queryset=Mission.objects.all(), required=False, allow_null=True, write_only=True)
    rtmp_server_url = serializers.CharField(read_only=True)
    rtmp_publish_url = serializers.CharField(read_only=True)
    hls_playback_url = serializers.CharField(read_only=True)
    stream_duration_seconds = serializers.SerializerMethodField()

    class Meta:
        model = Stream
        fields = (
            "id",
            "stream_name",
            "drone",
            "mission",
            "drone_id",
            "mission_id",
            "stream_key",
            "is_active",
            "is_live",
            "live_started_at",
            "last_activity_at",
            "rtmp_server_url",
            "rtmp_publish_url",
            "hls_playback_url",
            "stream_duration_seconds",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("stream_key", "is_live", "live_started_at", "last_activity_at", "created_at", "updated_at")

    def get_stream_duration_seconds(self, obj: Stream) -> int:
        return int(obj.stream_duration.total_seconds())
